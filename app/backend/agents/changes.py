"""Proposed changes to files: staged in memory, applied only after approval.

The editor never touches the disk. Each edit it asks for is checked and kept
in a ChangeSet (the staged text of each file). When the user approves the
diff, ``apply`` writes the files; ``undo_changes`` can put them back.

What is enforced here, on top of the Workspace's path rules:
  * an edit must match the file's text exactly, and exactly once
  * no secrets files, nothing inside .git / node_modules / .venv and the like,
    no binary, non-UTF-8, oversized or mixed-line-ending files
  * limits on files, edits and sizes
  * Windows (CRLF) or Unix line endings, and a byte-order mark, are preserved
  * applying is all-or-nothing: nothing is written if any file changed since
    the proposal, originals are backed up first, writes are atomic, and a
    failure part way rolls back what was already written
  * undo only touches files that still contain exactly what ORION wrote
"""

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.backend.agents.workspace import (
    IGNORED_DIRS,
    MAX_FILE_BYTES,
    Workspace,
    WorkspaceError,
    format_numbered_lines,
    is_secret_name,
)

MAX_FILES = 5
MAX_EDITS = 12
MAX_NEW_FILE_CHARS = 100_000
MAX_FILE_CHARS_AFTER_EDIT = MAX_FILE_BYTES
SNIPPET_CONTEXT_LINES = 2
SNIPPET_MAX_LINES = 24
HINT_MAX_CHARS = 700

MANIFEST = "manifest.json"

_NUMBERED = re.compile(r"^\s*\d+\| ?")


class ChangeError(Exception):
    """A change that cannot be made. The message is safe to show the model."""


class ScopeViolation(ChangeError):
    """An edit exceeds the approved plan; the run must stop."""


@dataclass
class FileChange:
    path: str                    # relative to the folder, forward slashes
    absolute: Path
    original: str | None         # None for a new file; text uses "\n" line ends
    current: str                 # the staged text, also with "\n" line ends
    newline: str                 # what to write: "\n" or "\r\n"
    bom: bool
    baseline_sha: str | None     # hash of the file's bytes when first staged
    edits: int = 0


@dataclass
class ApplyReport:
    written: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)


@dataclass
class UndoReport:
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (path, why)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".orion-", suffix=".tmp")

    try:
        with os.fdopen(handle, "wb") as temp:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())

        if path.exists():
            shutil.copymode(path, temp_name)

        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass

        raise


def _strip_numbers(find: str, replace: str) -> tuple[str, str]:
    """Models often copy the "  12| " line numbers read_file shows. Drop them."""
    lines = find.split("\n")
    meaningful = [line for line in lines if line.strip()]

    if meaningful and all(_NUMBERED.match(line) for line in meaningful):
        find = "\n".join(_NUMBERED.sub("", line, count=1) for line in lines)
        rep = replace.split("\n")
        rep_meaningful = [line for line in rep if line.strip()]

        if rep_meaningful and all(_NUMBERED.match(line) for line in rep_meaningful):
            replace = "\n".join(_NUMBERED.sub("", line, count=1) for line in rep)

    return find, replace


class ChangeSet:
    def __init__(
        self,
        workspace: Workspace,
        max_files: int = MAX_FILES,
        max_edits: int = MAX_EDITS,
        allowed_files: tuple[str, ...] | None = None,
    ) -> None:
        self.workspace = workspace
        self.max_files = max_files
        self.max_edits = max_edits
        self._changes: dict[str, FileChange] = {}
        self._edit_count = 0
        self.allowed_files = (frozenset(self._target(p)[0] for p in allowed_files)
                              if allowed_files is not None else None)

    # -- what is staged --------------------------------------------------------

    def files(self) -> list[FileChange]:
        return [c for c in self._changes.values() if c.current != (c.original or "") or c.original is None]

    def __bool__(self) -> bool:
        return bool(self.files())

    @property
    def edit_count(self) -> int:
        return self._edit_count

    def diff(self) -> str:
        """The staged changes as one unified diff, like ``git diff``."""
        parts: list[str] = []

        for change in self.files():
            old = (change.original or "").splitlines()
            new = change.current.splitlines()
            before = "/dev/null" if change.original is None else f"a/{change.path}"

            parts.extend(
                difflib.unified_diff(old, new, before, f"b/{change.path}", lineterm="", n=3)
            )

        return "\n".join(parts)

    def stats(self) -> tuple[int, int, int]:
        """(files changed, lines added, lines removed)."""
        added = removed = 0

        for line in self.diff().splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1

        return len(self.files()), added, removed

    # -- choosing a file --------------------------------------------------------

    def _target(self, path: str | None) -> tuple[str, Path]:
        if not path or not str(path).strip():
            raise ChangeError("A 'path' is needed.")

        try:
            resolved = self.workspace.resolve(path)
        except WorkspaceError as exc:
            raise ChangeError(str(exc)) from exc

        if resolved == self.workspace.root:
            raise ChangeError("That is the folder itself, not a file.")

        shown = self.workspace.display(resolved)
        parts = shown.split("/")

        for part in parts[:-1]:
            if part in IGNORED_DIRS:
                raise ChangeError(f"Files inside {part}/ are never edited.")

        if is_secret_name(parts[-1]):
            raise ChangeError(f"'{shown}' looks like a secrets file and is never edited.")

        if resolved.is_dir():
            raise ChangeError(f"'{shown}' is a folder, not a file.")

        return shown, resolved

    def _check_scope(self, shown: str) -> None:
        if self.allowed_files is not None and shown not in self.allowed_files:
            raise ScopeViolation(
                f"'{shown}' is outside the approved file scope. "
                "Stop and request a new plan; do not change another file instead."
            )

    def _load(self, shown: str, absolute: Path, requested: str) -> FileChange:
        """Read a file from disk for editing, refusing anything unsafe to rewrite."""
        if not absolute.exists():
            hint = self.workspace._did_you_mean(requested, "file")

            raise ChangeError(
                f"No such file: {requested}.{hint} To make a new file, use 'create'."
            )

        try:
            size = absolute.stat().st_size

            if size > MAX_FILE_BYTES:
                raise ChangeError(f"'{shown}' is too large to edit safely.")

            raw = absolute.read_bytes()
        except OSError as exc:
            raise ChangeError(f"Could not read '{shown}' ({exc.strerror}).") from exc

        if b"\x00" in raw[:4096]:
            raise ChangeError(f"'{shown}' is a binary file and is not edited.")

        bom = raw.startswith(b"\xef\xbb\xbf")

        try:
            text = (raw[3:] if bom else raw).decode("utf-8")
        except UnicodeDecodeError:
            raise ChangeError(f"'{shown}' is not UTF-8 text, so ORION will not edit it.") from None

        crlf = text.count("\r\n")
        lone_lf = text.count("\n") - crlf

        if crlf and lone_lf:
            raise ChangeError(
                f"'{shown}' mixes Windows and Unix line endings, so ORION will not rewrite it."
            )

        normalized = text.replace("\r\n", "\n")

        if "\r" in normalized:
            raise ChangeError(f"'{shown}' has unusual line endings, so ORION will not edit it.")

        return FileChange(
            path=shown,
            absolute=absolute,
            original=normalized,
            current=normalized,
            newline="\r\n" if crlf else "\n",
            bom=bom,
            baseline_sha=_sha(raw),
        )

    def _room_for(self, change: FileChange) -> None:
        """Enforce the limits before keeping a (new or first-touched) file."""
        if change.path not in self._changes and len(self._changes) >= self.max_files:
            raise ChangeError(f"At most {self.max_files} files can be changed in one task.")

        if self._edit_count >= self.max_edits:
            raise ChangeError(f"At most {self.max_edits} edits can be made in one task. Finish now.")

    # -- editing ---------------------------------------------------------------

    def replace(self, path: str | None, find: str | None, replace: str | None) -> str:
        """Replace ONE exact occurrence of ``find`` with ``replace`` in a file."""
        shown, absolute = self._target(path)
        self._check_scope(shown)
        change = self._changes.get(shown) or self._load(shown, absolute, str(path))

        find = (find or "").replace("\r\n", "\n")
        replace = (replace or "").replace("\r\n", "\n")
        find, replace = _strip_numbers(find, replace)

        if not find:
            raise ChangeError("'find' is empty. It must be the exact text to replace.")

        if find == replace:
            raise ChangeError("'find' and 'replace' are identical, so nothing would change.")

        self._room_for(change)

        count = change.current.count(find)

        if count == 0:
            raise ChangeError(self._not_found(shown, change.current, find))

        if count > 1:
            raise ChangeError(
                f"'find' matches {count} places in {shown}. "
                "Add a few surrounding lines so it matches exactly one."
            )

        index = change.current.index(find)
        updated = change.current[:index] + replace + change.current[index + len(find):]

        if len(updated) > MAX_FILE_CHARS_AFTER_EDIT:
            raise ChangeError(f"That would make {shown} too large.")

        first_line = change.current.count("\n", 0, index) + 1
        change.current = updated
        change.edits += 1
        self._changes[shown] = change
        self._edit_count += 1

        return f"Replaced 1 place in {shown}. It now reads:\n" + self._snippet(updated, first_line, replace)

    def create(self, path: str | None, content: str | None) -> str:
        """Stage a brand-new file (its path must not exist yet)."""
        shown, absolute = self._target(path)
        self._check_scope(shown)

        if shown in self._changes:
            raise ChangeError(f"'{shown}' was already changed in this task; use 'replace' to adjust it.")

        if absolute.exists():
            raise ChangeError(f"'{shown}' already exists. Use 'replace' to change it.")

        for parent in absolute.parents:
            if parent.exists():
                if not parent.is_dir():
                    raise ChangeError(f"'{self.workspace.display(parent)}' is a file, so '{shown}' cannot be created.")

                break

        text = (content or "").replace("\r\n", "\n")

        if not text.strip():
            raise ChangeError("A new file needs some 'content'.")

        if len(text) > MAX_NEW_FILE_CHARS:
            raise ChangeError(f"A new file can hold at most {MAX_NEW_FILE_CHARS} characters.")

        if not text.endswith("\n"):
            text += "\n"

        change = FileChange(
            path=shown, absolute=absolute, original=None, current=text,
            newline="\n", bom=False, baseline_sha=None, edits=1,
        )

        self._room_for(change)
        self._changes[shown] = change
        self._edit_count += 1

        lines = text.count("\n")

        return f"Created {shown} ({lines} line{'' if lines == 1 else 's'})."

    def read(self, path: str | None, start_line: int = 1) -> str | None:
        """The staged text of a file, numbered like read_file; None if not staged."""
        try:
            shown, _ = self._target(path)
        except ChangeError:
            return None

        change = self._changes.get(shown)

        if change is None:
            return None

        return format_numbered_lines(
            shown, change.current, start_line, note=" [including your staged changes]"
        )

    # -- helpers for messages -----------------------------------------------------

    @staticmethod
    def _snippet(text: str, first_line: int, replacement: str) -> str:
        lines = text.split("\n")
        changed = max(1, replacement.count("\n") + 1)
        start = max(1, first_line - SNIPPET_CONTEXT_LINES)
        end = min(len(lines), first_line + changed - 1 + SNIPPET_CONTEXT_LINES)

        shown = [f"{n:>5}| {lines[n - 1][:200]}" for n in range(start, end + 1)]

        if len(shown) > SNIPPET_MAX_LINES:
            shown = shown[:SNIPPET_MAX_LINES] + ["  ... (more lines changed)"]

        return "\n".join(shown)

    @staticmethod
    def _not_found(shown: str, text: str, find: str) -> str:
        lines = text.split("\n")
        wanted = [line.strip() for line in find.split("\n")]

        # Same lines, different spaces or indentation: show the exact text to copy.
        if len(wanted) <= len(lines):
            stripped = [line.strip() for line in lines]

            for start in range(len(lines) - len(wanted) + 1):
                if stripped[start : start + len(wanted)] == wanted:
                    block = "\n".join(lines[start : start + len(wanted)])

                    return (
                        f"'find' did not match {shown} exactly, but these lines "
                        f"(from line {start + 1}) match apart from spaces or indentation. "
                        f"Copy them exactly:\n{block}"[:HINT_MAX_CHARS]
                    )

        first = next((line for line in wanted if line), "")
        close = difflib.get_close_matches(first, [line.strip() for line in lines], n=1, cutoff=0.6)
        message = f"'find' was not found in {shown}."

        if close:
            number = next(i for i, line in enumerate(lines, 1) if line.strip() == close[0])
            message += f" The closest line is line {number}: {lines[number - 1].strip()[:200]}"

        message += " Use read_file to look at the file again and copy the text exactly, without the line numbers."

        return message[:HINT_MAX_CHARS]

    # -- applying ----------------------------------------------------------------

    def _content_bytes(self, change: FileChange) -> bytes:
        data = change.current.replace("\n", change.newline).encode("utf-8")

        return (b"\xef\xbb\xbf" + data) if change.bom else data

    def apply(self, backup_dir: Path) -> ApplyReport:
        """Write the staged changes to disk, all or nothing, keeping backups.

        Raises ``ChangeError`` (and writes nothing) if any file changed since
        the proposal; rolls back if a write fails part way.
        """
        changes = self.files()

        if not changes:
            raise ChangeError("There are no changes to apply.")

        # 1. Preflight: re-check every target and make sure nothing moved under us.
        plan: list[tuple[FileChange, Path]] = []

        for change in changes:
            self._check_scope(change.path)
            try:
                absolute = self.workspace.resolve(change.path)
            except WorkspaceError as exc:
                raise ChangeError(f"{change.path}: {exc}") from exc

            if change.original is None:
                if absolute.exists():
                    raise ChangeError(
                        f"{change.path} was created by someone else since the proposal; nothing was written."
                    )
            else:
                try:
                    current_sha = _sha(absolute.read_bytes())
                except OSError:
                    raise ChangeError(f"{change.path} can no longer be read; nothing was written.") from None

                if current_sha != change.baseline_sha:
                    raise ChangeError(
                        f"{change.path} changed since the proposal was made; nothing was written. "
                        "Run the task again."
                    )

            plan.append((change, absolute))

        # 2. Back up the originals and record what we are about to do.
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "files").mkdir(exist_ok=True)

        entries = []

        for index, (change, absolute) in enumerate(plan):
            data = self._content_bytes(change)
            entry = {
                "path": change.path,
                "existed": change.original is not None,
                "backup": None,
                "before_sha": change.baseline_sha,
                "after_sha": _sha(data),
                "created_dirs": [],
            }

            if change.original is not None:
                backup_name = f"files/{index}.bak"
                shutil.copyfile(absolute, backup_dir / backup_name)
                entry["backup"] = backup_name
            else:
                missing = [p for p in reversed(absolute.parents) if not p.exists() and self.workspace.root in p.parents]
                entry["created_dirs"] = [self.workspace.display(p) for p in missing]

            entries.append(entry)

        manifest = {
            "version": 1,
            "created": datetime.now(timezone.utc).isoformat(),
            "root": str(self.workspace.root),
            "state": "pending",
            "undone": False,
            "files": entries,
        }
        _write_manifest(backup_dir, manifest)

        # 3. Write each file; on any failure put everything back.
        report = ApplyReport()
        done: list[int] = []

        try:
            for index, (change, absolute) in enumerate(plan):
                _atomic_write(absolute, self._content_bytes(change))
                done.append(index)
                (report.created if change.original is None else report.written).append(change.path)
        except Exception as exc:
            problems = _rollback(plan, entries, backup_dir, done, self.workspace)
            manifest["state"] = "rolled_back"
            _write_manifest(backup_dir, manifest)

            raise ChangeError(
                f"Writing failed ({exc}); the files were put back as they were."
                + (f" Could not restore: {', '.join(problems)}." if problems else "")
            ) from exc

        manifest["state"] = "applied"
        _write_manifest(backup_dir, manifest)

        return report


# -- backups, manifest and undo --------------------------------------------------

def _write_manifest(backup_dir: Path, manifest: dict) -> None:
    _atomic_write(backup_dir / MANIFEST, json.dumps(manifest, indent=2).encode("utf-8"))


def read_manifest(backup_dir: Path) -> dict | None:
    try:
        data = json.loads((backup_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    return data if isinstance(data, dict) and isinstance(data.get("files"), list) else None


def _rollback(plan, entries, backup_dir: Path, done: list[int], workspace: Workspace) -> list[str]:
    """Undo writes made so far. Returns the paths that could not be restored."""
    problems: list[str] = []

    for index in reversed(done):
        change, absolute = plan[index]

        try:
            if change.original is None:
                absolute.unlink(missing_ok=True)
            else:
                _atomic_write(absolute, (backup_dir / entries[index]["backup"]).read_bytes())
        except OSError:
            problems.append(change.path)

    # Folders made for new files go too, if they are empty again.
    for entry in entries:
        for name in reversed(entry["created_dirs"]):
            try:
                folder = workspace.resolve(name)

                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            except (WorkspaceError, OSError):
                pass

    return problems


def changes_summary(backup_dir: Path) -> dict | None:
    """What the page needs to know about a run's applied changes, or None."""
    manifest = read_manifest(backup_dir)

    if manifest is None or manifest.get("state") not in ("applied", "pending"):
        return None

    return {
        "applied": True,
        "undone": bool(manifest.get("undone")),
        "files": [entry["path"] for entry in manifest["files"]],
        "created": manifest.get("created"),
    }


def backup_dir_for(run_id: str, data_dir: Path) -> Path:
    """Where a run's backups and undo record are kept (inside ORION's data folder)."""
    return Path(data_dir) / "run_changes" / run_id


def discard_backups(backup_dir: Path) -> None:
    shutil.rmtree(backup_dir, ignore_errors=True)


def undo_changes(backup_dir: Path, blocked: list[Path] | None = None) -> UndoReport:
    """Put back what ``apply`` changed, but only where the file is still ORION's.

    A file the user has edited since is left alone and reported.
    """
    manifest = read_manifest(backup_dir)

    if manifest is None or manifest.get("state") not in ("applied", "pending"):
        raise ChangeError("There are no applied changes to undo.")

    if manifest.get("undone"):
        raise ChangeError("These changes were already undone.")

    try:
        workspace = Workspace(manifest["root"], blocked=blocked or [])
    except WorkspaceError as exc:
        raise ChangeError(f"The folder is no longer available: {exc}") from exc

    report = UndoReport()

    for entry in manifest["files"]:
        shown = entry["path"]

        try:
            absolute = workspace.resolve(shown)
        except WorkspaceError as exc:
            report.skipped.append((shown, str(exc)))
            continue

        try:
            current = _sha(absolute.read_bytes()) if absolute.exists() else None
        except OSError:
            report.skipped.append((shown, "could not be read"))
            continue

        if entry["existed"]:
            if current == entry["after_sha"]:
                try:
                    _atomic_write(absolute, (backup_dir / entry["backup"]).read_bytes())
                except OSError as exc:
                    report.skipped.append((shown, f"could not be restored ({exc.strerror})"))
                    continue

                report.restored.append(shown)
            elif current == entry["before_sha"]:
                report.restored.append(shown)         # already back to the original
            else:
                report.skipped.append((shown, "was changed after ORION wrote it, so it was left alone"))
        else:
            if current == entry["after_sha"]:
                try:
                    absolute.unlink()
                except OSError as exc:
                    report.skipped.append((shown, f"could not be deleted ({exc.strerror})"))
                    continue

                report.deleted.append(shown)
            elif current is None:
                report.deleted.append(shown)          # already gone
            else:
                report.skipped.append((shown, "was changed after ORION created it, so it was left alone"))

        # Remove folders ORION made for a new file, if they are empty again.
        for name in reversed(entry.get("created_dirs", [])):
            try:
                folder = workspace.resolve(name)

                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            except (WorkspaceError, OSError):
                pass

    manifest["undone"] = True
    manifest["undone_at"] = datetime.now(timezone.utc).isoformat()
    manifest["undo_skipped"] = [list(item) for item in report.skipped]
    _write_manifest(backup_dir, manifest)

    return report
