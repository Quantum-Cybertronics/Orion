"""Text helpers for conversations: titles, search snippets and Markdown export.

Nothing here touches the database, so it is easy to test on its own.
"""

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from urllib.parse import quote

MAX_TITLE_LENGTH = 100

SNIPPET_BEFORE = 40  # characters of context shown before a search match
SNIPPET_AFTER = 90   # ...and after it


def clean_title(title: str) -> str:
    """Collapse whitespace and validate a user-chosen title.

    Raises ``ValueError`` (with a message safe to show) if it is unusable.
    """
    cleaned = " ".join(title.split())

    if not cleaned:
        raise ValueError("The title cannot be empty.")

    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValueError(f"The title can be at most {MAX_TITLE_LENGTH} characters.")

    return cleaned


def normalize_query(query: str) -> str:
    """The form of a search query that is actually matched (lower case)."""
    return " ".join(query.split()).lower()


LIKE_ESCAPE = "!"


def like_pattern(needle: str) -> str:
    """A SQL ``LIKE`` pattern matching ``needle`` literally anywhere.

    ``%`` and ``_`` are wildcards in LIKE, so they are escaped (with
    ``LIKE_ESCAPE``): searching for "100%" must not match everything.
    """
    escaped = (
        needle.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )

    return f"%{escaped}%"


def make_snippet(text: str, needle: str) -> str:
    """A short excerpt of ``text`` around the first occurrence of ``needle``."""
    flat = " ".join(text.split())
    index = flat.lower().find(needle) if needle else -1

    if index < 0:
        end = SNIPPET_BEFORE + SNIPPET_AFTER
        return flat[:end] + ("\u2026" if len(flat) > end else "")

    start = max(0, index - SNIPPET_BEFORE)
    end = min(len(flat), index + len(needle) + SNIPPET_AFTER)

    return (
        ("\u2026" if start > 0 else "")
        + flat[start:end]
        + ("\u2026" if end < len(flat) else "")
    )


# -- export -----------------------------------------------------------------

_ROLE_HEADINGS = {"user": "You", "assistant": "ORION"}


def build_markdown(
    title: str,
    messages: Sequence,
    attachment_names: Iterable[str],
    exported_at: datetime,
) -> str:
    """Render a conversation as a Markdown document.

    ``messages`` need ``.role`` and ``.content``. System messages and file
    contents are never included, only the names of attached files.
    """
    shown = [m for m in messages if m.role in _ROLE_HEADINGS]
    names = [name for name in attachment_names if name]

    lines = [f"# {title}", ""]

    count = len(shown)
    lines.append(
        f"*Exported from ORION on {exported_at:%Y-%m-%d %H:%M} "
        f"\u00b7 {count} message{'' if count == 1 else 's'}*"
    )

    if names:
        lines += ["", "Attached files: " + ", ".join(f"`{n}`" for n in names)]

    for message in shown:
        lines += [
            "",
            "---",
            "",
            f"## {_ROLE_HEADINGS[message.role]}",
            "",
            message.content.strip("\n"),
        ]

    return "\n".join(lines) + "\n"


def content_disposition(title: str) -> str:
    """A ``Content-Disposition`` header value for downloading the export.

    Gives an ASCII fallback name plus the full Unicode name (RFC 5987), so
    titles in any language save under a sensible file name.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:50]
    ascii_name = f"orion-{slug or 'conversation'}.md"

    readable = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title)
    readable = " ".join(readable.split())[:80] or "conversation"

    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(readable + '.md')}"
    )
