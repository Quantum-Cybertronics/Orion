from datetime import datetime
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.backend.conversations.formatting import (
    MAX_TITLE_LENGTH,
    build_markdown,
    clean_title,
    content_disposition,
    like_pattern,
    make_snippet,
    normalize_query,
)


def message(role: str, content: str):
    return SimpleNamespace(role=role, content=content)


# ----------------------------------------------------------------- titles

def test_clean_title_collapses_whitespace():
    assert clean_title("  My \n  plan\tfor   Pune  ") == "My plan for Pune"


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_clean_title_rejects_empty(bad):
    with pytest.raises(ValueError):
        clean_title(bad)


def test_clean_title_length_limit():
    assert clean_title("x" * MAX_TITLE_LENGTH) == "x" * MAX_TITLE_LENGTH

    with pytest.raises(ValueError):
        clean_title("x" * (MAX_TITLE_LENGTH + 1))


# ----------------------------------------------------------------- search

def test_normalize_query():
    assert normalize_query("  Hello \n  WORLD ") == "hello world"


def test_like_pattern_matches_anywhere_and_escapes_wildcards():
    assert like_pattern("abc") == "%abc%"
    assert like_pattern("100%") == "%100!%%"
    assert like_pattern("a_b") == "%a!_b%"
    assert like_pattern("wow!") == "%wow!!%"


def test_like_pattern_really_matches_literally_in_sqlite():
    """Run the pattern through a real LIKE ... ESCAPE to prove it."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("create table t (c text)")
    db.executemany("insert into t values (?)", [("it is 100% sure",), ("plain words",), ("a_b",), ("axb",), ("WOW!",)])

    def hits(needle):
        rows = db.execute(
            "select c from t where c like ? escape '!' order by rowid", (like_pattern(needle.lower()),)
        )
        return [r[0] for r in rows]

    assert hits("100%") == ["it is 100% sure"]
    assert hits("%") == ["it is 100% sure"]        # not "everything"
    assert hits("_") == ["a_b"]                    # not "any one character"
    assert hits("a_b") == ["a_b"]                  # and not "axb"
    assert hits("wow!") == ["WOW!"]
    assert hits("WORDS") == ["plain words"]        # case-insensitive


def test_snippet_surrounds_the_match_with_ellipses():
    text = ("a" * 100) + " NEEDLE " + ("b" * 200)
    snippet = make_snippet(text, "needle")

    assert "NEEDLE" in snippet
    assert snippet.startswith("\u2026") and snippet.endswith("\u2026")
    assert len(snippet) < len(text)


def test_snippet_of_short_text_is_unchanged():
    assert make_snippet("find the needle here", "needle") == "find the needle here"


def test_snippet_flattens_newlines():
    assert make_snippet("line one\n\nline   two", "two") == "line one line two"


def test_snippet_without_a_match_falls_back_to_the_start():
    assert make_snippet("hello", "zzz") == "hello"


# ----------------------------------------------------------------- export

NOW = datetime(2026, 10, 9, 14, 5)


def test_markdown_export_layout():
    text = build_markdown(
        "Trip to Goa",
        [message("user", "Plan a trip"), message("assistant", "Sure!\n\n- beach")],
        [],
        NOW,
    )

    assert text.startswith("# Trip to Goa\n\n*Exported from ORION on 2026-10-09 14:05")
    assert "2 messages*" in text
    assert "## You\n\nPlan a trip" in text
    assert "## ORION\n\nSure!\n\n- beach" in text
    assert text.index("## You") < text.index("## ORION")
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_markdown_export_lists_attachment_names_but_not_contents():
    text = build_markdown("T", [message("user", "hi")], ["notes.txt", "a.pdf"], NOW)

    assert "Attached files: `notes.txt`, `a.pdf`" in text
    assert "1 message*" in text  # singular


def test_markdown_export_skips_system_messages():
    text = build_markdown(
        "T",
        [message("system", "SECRET FILE TEXT"), message("user", "hi")],
        [],
        NOW,
    )

    assert "SECRET FILE TEXT" not in text


def test_markdown_export_of_an_empty_conversation():
    text = build_markdown("Empty", [], [], NOW)

    assert text.startswith("# Empty")
    assert "0 messages" in text and "## You" not in text


# --------------------------------------------------------------- filename

def test_download_name_is_an_ascii_slug_with_a_unicode_variant():
    header = content_disposition("Plan: Q4 / budget?")

    assert header.startswith('attachment; filename="orion-plan-q4-budget.md"')
    assert "filename*=UTF-8''" in header
    # path separators and other unsafe characters never reach the file name
    encoded = header.split("filename*=UTF-8''")[1]
    assert "/" not in unquote(encoded) and "?" not in unquote(encoded)


def test_download_name_for_a_non_latin_title():
    header = content_disposition("\u092e\u093e\u091d\u0940 \u092f\u094b\u091c\u0928\u093e")

    assert 'filename="orion-conversation.md"' in header  # ASCII fallback
    assert unquote(header.split("filename*=UTF-8''")[1]) == (
        "\u092e\u093e\u091d\u0940 \u092f\u094b\u091c\u0928\u093e.md"
    )
    header.encode("latin-1")  # header values must be latin-1 safe


def test_download_name_survives_a_title_of_only_symbols():
    header = content_disposition('???"')

    assert 'filename="orion-conversation.md"' in header
    assert unquote(header.split("filename*=UTF-8''")[1]) == "conversation.md"
