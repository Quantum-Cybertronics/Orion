"""A read-only window onto one folder.

Everything the reader (and later, other agents) can see of the user's files
goes through this class. It is deliberately strict, because file names and
contents are untrusted input: a path chosen by a model, or text inside a file,
must never be able to reach anything outside the folder the user picked.

What it enforces:
  * only paths inside the chosen folder, after following symlinks
  * no absolute paths, ``..``, drive letters, ``:`` (Windows data streams)
    or Windows device names (``CON``, ``NUL``...)
  * some places are always off limits (ORION's own data folder)
  * secrets-looking files (``.env``, private keys...) are never shown
  * version-control and dependency folders are skipped
  * hard caps on file size, output size, entries and search effort
"""

import os
import re
import time
from collections.abc import Sequence
from pathlib import Path

IGNORED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea",
    }
)

SENSITIVE_NAMES = frozenset(
    {".env", ".netrc", ".pgpass", ".npmrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
)
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".ppk")

_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

MAX_FILE_BYTES = 1_000_000     # bigger files are not read or searched
MAX_READ_CHARS = 2500          # per read_file result (keeps the model's context small)
MAX_READ_LINES = 120
MAX_LINE_CHARS = 300           # one very long line (minified code) is cut here
MAX_LIST_ENTRIES = 80
MAX_OVERVIEW_ENTRIES = 50
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_FILES = 2000
MAX_SEARCH_SECONDS = 3.0
MAX_QUERY_CHARS = 200
SNIPPET_CHARS = 160
SUGGESTIONS = 3                # "did you mean" candidates offered for a wrong path
SUGGEST_MAX_ENTRIES = 5000
SUGGEST_MAX_SECONDS = 1.0


class WorkspaceError(Exception):
    """A request the workspace refuses. The message is safe to show the model."""


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"

    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    return f"{size / (1024 * 1024):.1f} MB"


def _looks_sensitive(name: str) -> bool:
    lowered = name.lower()

    return (
        lowered in SENSITIVE_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith(SENSITIVE_SUFFIXES)
    )


def is_secret_name(name: str) -> bool:
    """Whether a file name looks like it holds secrets (never shown or edited)."""
    return _looks_sensitive(name)


def format_numbered_lines(
    shown_name: str,
    text: str,
    start_line: int = 1,
    max_lines: int = MAX_READ_LINES,
    note: str = "",
) -> str:
    """The numbered, size-capped view of ``text`` that read_file returns."""
    lines = text.splitlines()
    total = len(lines)

    if total == 0:
        return f"{shown_name}: (empty file)"

    start = max(1, int(start_line))

    if start > total:
        raise WorkspaceError(f"'{shown_name}' has only {total} lines.")

    limit = max(1, min(int(max_lines), MAX_READ_LINES))
    out: list[str] = []
    used = 0
    last = start - 1

    for number in range(start, min(total, start + limit - 1) + 1):
        line = lines[number - 1]

        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " ..."

        rendered = f"{number:>5}| {line}"

        # Always show at least one line, then stop at the size cap.
        if out and used + len(rendered) > MAX_READ_CHARS:
            break

        out.append(rendered)
        used += len(rendered) + 1
        last = number

    header = f"{shown_name} (lines {start}-{last} of {total}){note}"
    footer = f"\n[More: call read_file with start_line={last + 1}]" if last < total else ""

    return header + "\n" + "\n".join(out) + footer


def _decode(raw: bytes) -> str | None:
    """Text of a file, or ``None`` if it looks binary."""
    if b"\x00" in raw[:4096]:
        return None

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # Older Windows text files are often cp1252 rather than UTF-8.
    text = raw.decode("cp1252", errors="replace")

    if text and text.count("\ufffd") / len(text) > 0.05:
        return None

    return text


class Workspace:
    def __init__(self, root: Path | str, blocked: Sequence[Path | str] = ()) -> None:
        self.root = Path(root).resolve()

        if not self.root.is_dir():
            raise WorkspaceError("That is not a folder.")

        self.blocked = [Path(path).resolve() for path in blocked]

        if self._is_blocked(self.root):
            raise WorkspaceError("That folder is off limits.")

    # -- path safety ---------------------------------------------------------

    def _is_blocked(self, resolved: Path) -> bool:
        return any(resolved == b or b in resolved.parents for b in self.blocked)

    def _is_inside(self, resolved: Path) -> bool:
        return resolved == self.root or self.root in resolved.parents

    def resolve(self, relative: str | None) -> Path:
        """Turn a model-supplied path into a real path inside the folder."""
        text = (relative or ".").strip().replace("\\", "/")

        if "\x00" in text:
            raise WorkspaceError("That path is not valid.")

        if text.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", text):
            raise WorkspaceError(
                "Use a path relative to the folder, like 'src/app.py' (not an absolute path)."
            )

        parts = [part for part in text.split("/") if part not in ("", ".")]

        for part in parts:
            if part == "..":
                raise WorkspaceError("'..' is not allowed; stay inside the folder.")

            if ":" in part:
                raise WorkspaceError("Paths cannot contain ':'.")

            if part.split(".")[0].rstrip(" ").upper() in _WINDOWS_DEVICES:
                raise WorkspaceError(f"'{part}' is a reserved name and cannot be used.")

        try:
            resolved = self.root.joinpath(*parts).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceError("That path could not be resolved.") from exc

        if not self._is_inside(resolved):
            raise WorkspaceError("That path leads outside the folder.")

        if self._is_blocked(resolved):
            raise WorkspaceError("That location is off limits.")

        return resolved

    def display(self, resolved: Path) -> str:
        """The path as shown to the model: relative, with forward slashes."""
        if resolved == self.root:
            return "."

        return resolved.relative_to(self.root).as_posix()

    def _visible(self, path: Path, *, is_dir: bool) -> bool:
        """Whether a directory entry should appear in listings and searches."""
        name = path.name

        if is_dir and name in IGNORED_DIRS:
            return False

        if not is_dir and _looks_sensitive(name):
            return False

        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False

        return self._is_inside(resolved) and not self._is_blocked(resolved)

    def _did_you_mean(self, requested: str | None, kind: str) -> str:
        """A hint for a path that does not exist, e.g. ``backend/`` -> ``app/backend/``.

        Models usually get the *name* right but leave out a parent folder.
        ``kind`` is "dir", "file" or "any". Returns "" if nothing matches.
        """
        parts = [
            part
            for part in (requested or "").replace("\\", "/").split("/")
            if part not in ("", ".", "..")
        ]

        if not parts:
            return ""

        target = "/".join(parts).lower()
        name = parts[-1].lower()
        by_path: list[str] = []
        by_name: list[str] = []
        seen = 0
        deadline = time.monotonic() + SUGGEST_MAX_SECONDS

        try:
            for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
                here = Path(directory)

                dirnames[:] = sorted(d for d in dirnames if self._visible(here / d, is_dir=True))

                entries: list[tuple[str, bool]] = []

                if kind in ("dir", "any"):
                    entries += [(d, True) for d in dirnames]

                if kind in ("file", "any"):
                    entries += [
                        (f, False) for f in sorted(filenames)
                        if self._visible(here / f, is_dir=False)
                    ]

                for entry_name, is_dir in entries:
                    seen += 1

                    if seen > SUGGEST_MAX_ENTRIES or time.monotonic() > deadline:
                        raise StopIteration

                    relative = (here / entry_name).relative_to(self.root).as_posix()
                    shown = relative + ("/" if is_dir else "")

                    if ("/" + relative.lower()).endswith("/" + target):
                        by_path.append(shown)
                    elif entry_name.lower() == name:
                        by_name.append(shown)
        except StopIteration:
            pass
        except OSError:
            return ""

        hits = (by_path or by_name)[:SUGGESTIONS]

        if not hits:
            return ""

        return " Did you mean " + (hits[0] if len(hits) == 1 else "one of: " + ", ".join(hits)) + "?"

    # -- tools ---------------------------------------------------------------

    def list_files(self, path: str | None = ".") -> str:
        target = self.resolve(path)

        if target.is_file():
            raise WorkspaceError(f"'{self.display(target)}' is a file; use read_file.")

        if not target.is_dir():
            raise WorkspaceError(f"No such folder: {path}." + self._did_you_mean(path, "dir"))

        folders: list[str] = []
        files: list[str] = []

        try:
            entries = sorted(os.scandir(target), key=lambda e: e.name.lower())
        except OSError as exc:
            raise WorkspaceError(f"Could not read that folder ({exc.strerror}).") from exc

        for entry in entries:
            entry_path = Path(entry.path)

            try:
                is_dir = entry.is_dir()
            except OSError:
                continue

            if not self._visible(entry_path, is_dir=is_dir):
                continue

            if is_dir:
                folders.append(f"{entry.name}/")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0

                files.append(f"{entry.name} ({_human_size(size)})")

        shown = folders + files
        name = self.display(target)
        header = f"Contents of {name if name == '.' else name + '/'}: {len(folders)} folders, {len(files)} files"

        if not shown:
            return header + "\n(empty)"

        lines = shown[:MAX_LIST_ENTRIES]

        if len(shown) > MAX_LIST_ENTRIES:
            lines.append(f"... and {len(shown) - MAX_LIST_ENTRIES} more (list a subfolder to see them)")

        return header + "\n" + "\n".join(lines)

    def overview(self, max_entries: int = MAX_OVERVIEW_ENTRIES, depth: int = 2) -> str:
        """An indented tree of the top of the folder, shown to the model up front."""
        lines: list[str] = []
        budget = [max_entries]
        omitted = [0]

        def walk(directory: Path, level: int) -> None:
            try:
                entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError:
                return

            for entry in entries:
                entry_path = Path(entry.path)

                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue

                if not self._visible(entry_path, is_dir=is_dir):
                    continue

                if budget[0] <= 0:
                    omitted[0] += 1
                    continue

                budget[0] -= 1
                indent = "  " * level

                if is_dir:
                    lines.append(f"{indent}{entry.name}/")

                    if level + 1 < depth:
                        walk(entry_path, level + 1)
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0

                    lines.append(f"{indent}{entry.name} ({_human_size(size)})")

        walk(self.root, 0)

        if omitted[0]:
            lines.append("... and more (use list_files to explore)")

        return "\n".join(lines) if lines else "(the folder is empty)"

    def read_file(self, path: str, start_line: int = 1, max_lines: int = MAX_READ_LINES) -> str:
        target = self.resolve(path)
        shown_name = self.display(target)

        if not target.exists():
            raise WorkspaceError(f"No such file: {path}." + self._did_you_mean(path, "file"))

        if target.is_dir():
            raise WorkspaceError(f"'{shown_name}' is a folder; use list_files.")

        if _looks_sensitive(target.name):
            raise WorkspaceError(f"'{shown_name}' looks like a secrets file and is not shown.")

        try:
            size = target.stat().st_size

            if size > MAX_FILE_BYTES:
                raise WorkspaceError(
                    f"'{shown_name}' is too large to read ({_human_size(size)}). "
                    "Use search_files to find the part you need."
                )

            raw = target.read_bytes()
        except OSError as exc:
            raise WorkspaceError(f"Could not read '{shown_name}' ({exc.strerror}).") from exc

        text = _decode(raw)

        if text is None:
            raise WorkspaceError(f"'{shown_name}' is a binary file and cannot be shown as text.")

        return format_numbered_lines(shown_name, text, start_line, max_lines)

    def search_files(
        self,
        query: str,
        path: str | None = ".",
        max_results: int = MAX_SEARCH_RESULTS,
    ) -> str:
        """Case-insensitive plain-text search (not a regex, so it cannot hang)."""
        needle = " ".join((query or "").split()).lower()

        if not needle:
            raise WorkspaceError("Give some text to search for.")

        if len(needle) > MAX_QUERY_CHARS:
            raise WorkspaceError(f"The search text is too long (limit {MAX_QUERY_CHARS} characters).")

        base = self.resolve(path)

        if not base.exists():
            raise WorkspaceError(f"No such file or folder: {path}." + self._did_you_mean(path, "any"))

        limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
        matches: list[str] = []
        scanned = 0
        stopped = ""
        deadline = time.monotonic() + MAX_SEARCH_SECONDS

        def candidates():
            if base.is_file():
                yield base
                return

            for directory, dirnames, filenames in os.walk(base, followlinks=False):
                here = Path(directory)

                dirnames[:] = sorted(
                    d for d in dirnames if self._visible(here / d, is_dir=True)
                )

                for name in sorted(filenames):
                    yield here / name

        for file in candidates():
            if not self._visible(file, is_dir=False):
                continue

            if scanned >= MAX_SEARCH_FILES:
                stopped = f"Only the first {MAX_SEARCH_FILES} files were searched."
                break

            if time.monotonic() > deadline:
                stopped = "The search took too long and was stopped early."
                break

            try:
                if file.stat().st_size > MAX_FILE_BYTES:
                    continue

                text = _decode(file.read_bytes())
            except OSError:
                continue

            scanned += 1

            if text is None:
                continue

            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    snippet = " ".join(line.split())[:SNIPPET_CHARS]
                    matches.append(f"{self.display(file)}:{number}: {snippet}")

                    if len(matches) >= limit:
                        stopped = f"Stopped after {limit} matches; narrow the search for more."
                        break

            if len(matches) >= limit:
                break

        where = self.display(base)

        if not matches:
            note = f" {stopped}" if stopped else ""
            return f'No matches for "{query.strip()}" in {where} ({scanned} files searched).{note}'

        header = f'{len(matches)} match{"" if len(matches) == 1 else "es"} for "{query.strip()}":'
        footer = f"\n{stopped}" if stopped else ""

        return header + "\n" + "\n".join(matches) + footer
