import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from gbnf_check import Grammar  # noqa: E402

from app.backend.agents.changes import ChangeSet  # noqa: E402
from app.backend.agents.editor import (  # noqa: E402
    ACTIONS,
    SYSTEM_PROMPT,
    Editor,
    build_edit_schema,
    parse_edit_action,
)
from app.backend.agents.supervisor import RunCancelled, StepFailed  # noqa: E402
from app.backend.agents.workspace import Workspace  # noqa: E402
from app.backend.ai.base import AIProviderError  # noqa: E402
from app.backend.ai.gbnf import schema_to_gbnf  # noqa: E402

APP = "import os\n\ndef main():\n    print('hello world')\n    return 1\n"


def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(APP, newline="\n")
    (root / ".env").write_text("SECRET=1\n")
    (tmp_path / "outside.txt").write_text("outside\n")
    return root


class Script:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, schema):
        self.calls.append(([dict(m) for m in messages], schema))
        reply = self.replies.pop(0)

        if isinstance(reply, Exception):
            raise reply

        return reply if isinstance(reply, str) else json.dumps(reply)


def act(action, why="because", **args):
    return {"why": why, "action": action, **args}


def make(tmp_path, *replies, **kwargs):
    root = project(tmp_path)
    changes = ChangeSet(Workspace(root))
    notes = []
    script = Script(*replies)
    options = dict(
        notify=lambda title, detail, ms: notes.append((title, detail, ms)),
        check_cancelled=lambda: None,
        seconds_left=lambda: 100.0,
    )
    options.update(kwargs)

    return Editor(changes, "Make main return 2", script, **options), changes, script, notes, root


# ------------------------------------------------------------------- the loop

def test_reads_edits_and_finishes_without_touching_the_disk(tmp_path):
    editor, changes, script, notes, root = make(
        tmp_path,
        act("read_file", "look first", path="src/app.py"),
        act("replace", "change it", path="src/app.py", find="    return 1", replace="    return 2"),
        act("finish", "done", summary="main now returns 2."),
    )

    result = editor.run()

    assert result.summary == "main now returns 2." and result.tool_calls == 2
    assert [n[0] for n in notes] == ["read_file src/app.py", "replace src/app.py"]
    assert notes[1][1].startswith("Why: change it") and "return 2" in notes[1][1]
    assert changes.stats() == (1, 1, 1) and "+    return 2" in changes.diff()
    assert (root / "src" / "app.py").read_text() == APP                      # NOTHING was written


def test_the_model_is_shown_the_request_and_the_changed_lines(tmp_path):
    editor, _, script, _, _ = make(
        tmp_path,
        act("replace", path="src/app.py", find="return 1", replace="return 2"),
        act("finish", summary="ok"),
    )
    editor.run()

    first, second = script.calls[0][0], script.calls[1][0]

    assert first[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert "Request: Make main return 2" in first[1]["content"] and "src/" in first[1]["content"]
    assert "up to 14 tool calls" in first[1]["content"]
    assert "Replaced 1 place in src/app.py. It now reads:" in second[-1]["content"]
    assert "    5|     return 2" in second[-1]["content"]


def test_reading_a_file_shows_the_staged_version(tmp_path):
    editor, _, script, _, _ = make(
        tmp_path,
        act("replace", path="src/app.py", find="return 1", replace="return 2"),
        act("read_file", path="src/app.py"),
        act("finish", summary="ok"),
    )
    editor.run()

    seen = script.calls[2][0][-1]["content"]

    assert "[including your staged changes]" in seen and "return 2" in seen


def test_a_new_file_can_be_created(tmp_path):
    editor, changes, _, notes, root = make(
        tmp_path,
        act("create", path="src/util.py", content="def helper():\n    return 42\n"),
        act("finish", summary="added util.py"),
    )

    editor.run()

    assert notes[0][0] == "create src/util.py"
    assert "+++ b/src/util.py" in changes.diff() and not (root / "src" / "util.py").exists()


def test_finishing_with_no_edits_is_allowed(tmp_path):
    editor, changes, _, _, _ = make(tmp_path, act("finish", summary="The request cannot be done with these files."))

    result = editor.run()

    assert result.summary.startswith("The request cannot") and not changes


# --------------------------------------------------- refusals reach the model

def test_a_failed_edit_is_explained_so_the_model_can_correct_it(tmp_path):
    editor, changes, script, notes, _ = make(
        tmp_path,
        act("replace", path="src/app.py", find="return 3", replace="return 2"),
        act("replace", path="src/app.py", find="return 1", replace="return 2"),
        act("finish", summary="ok"),
    )

    editor.run()

    assert notes[0][0] == "replace src/app.py (error)"
    assert "'find' was not found in src/app.py" in script.calls[1][0][-1]["content"]
    assert changes.stats() == (1, 1, 1)                                       # the corrected edit worked


@pytest.mark.parametrize(
    "path", [".env", "../outside.txt", "/etc/passwd", ".git/config", "src"]
)
def test_dangerous_or_impossible_targets_never_get_staged(tmp_path, path):
    editor, changes, script, notes, _ = make(
        tmp_path,
        act("replace", path=path, find="a", replace="b"),
        act("create", path=path, content="x\n"),
        act("finish", summary="gave up"),
    )

    editor.run()

    assert not changes
    assert all(n[0].endswith("(error)") for n in notes)
    assert "Error:" in script.calls[1][0][-1]["content"]
    assert "SECRET" not in str(script.calls)


def test_missing_arguments_are_explained(tmp_path):
    editor, _, script, _, _ = make(
        tmp_path,
        act("replace"),
        act("create", path="x.txt"),
        act("read_file"),
        act("search_files"),
        act("finish", summary="ok"),
    )

    editor.run()

    results = [call[0][-1]["content"] for call in script.calls[1:]]

    assert "A 'path' is needed" in results[0]
    assert "needs some 'content'" in results[1]
    assert "read_file needs a 'path'" in results[2]
    assert "search_files needs a 'query'" in results[3]


def test_the_edit_limit_is_reported_to_the_model(tmp_path):
    root = project(tmp_path)
    changes = ChangeSet(Workspace(root), max_edits=1)
    script = Script(
        act("replace", path="src/app.py", find="return 1", replace="return 2"),
        act("replace", path="src/app.py", find="return 2", replace="return 3"),
        act("finish", summary="ok"),
    )
    Editor(changes, "r", script, notify=lambda *a: None, check_cancelled=lambda: None, seconds_left=lambda: 100.0).run()

    assert "At most 1 edits" in script.calls[2][0][-1]["content"]


def test_an_identical_edit_is_not_run_twice(tmp_path):
    edit = act("replace", path="src/app.py", find="return 1", replace="return 2")
    editor, changes, script, notes, _ = make(tmp_path, edit, edit, act("finish", summary="ok"))

    editor.run()

    assert "already made this exact call" in script.calls[2][0][-1]["content"]
    assert notes[1][0] == "replace src/app.py (error)"
    assert changes.edit_count == 1


def test_the_tool_call_cap_forces_a_finish_with_a_summary(tmp_path):
    reads = [act("read_file", path="src/app.py", start_line=n) for n in (1, 2, 3)]
    editor, _, script, _, _ = make(tmp_path, *reads, act("finish", summary="stopped"), max_tool_calls=3)

    result = editor.run()

    assert result.summary == "stopped"
    schema = script.calls[-1][1]
    assert schema["properties"]["action"]["enum"] == ["finish"]
    assert schema["required"] == ["why", "action", "summary"]
    assert "Finish now" in script.calls[-1][0][-1]["content"]


def test_asking_for_another_edit_after_the_cap_is_not_obeyed(tmp_path):
    editor, changes, script, _, _ = make(
        tmp_path,
        act("read_file", path="src/app.py"),
        act("replace", path="src/app.py", find="return 1", replace="return 2"),      # ignores "finish now"
        act("finish", summary="fine"),
        max_tool_calls=1,
    )

    assert editor.run().summary == "fine"
    assert not changes                                                                # that edit was never made


# -------------------------------------------------------- misbehaving models

def test_a_bad_reply_gets_one_chance_then_fails(tmp_path):
    ok, _, script, _, _ = make(tmp_path, "not json", act("finish", summary="recovered"))
    assert ok.run().summary == "recovered"
    assert "not a valid action: it was not valid JSON" in script.calls[1][0][-1]["content"]

    broken, _, _, _, _ = make(tmp_path, '{"why": "x", "action": "format_disk"}', "{}")

    with pytest.raises(StepFailed) as error:
        broken.run()

    assert "did not give a usable action" in str(error.value)


@pytest.mark.parametrize(
    "bad",
    ["[1]", '{"why": "x"}', '{"why": "x", "action": "answer", "answer": "hi"}',
     '{"why": "x", "action": "finish"}', '{"why": "x", "action": "finish", "summary": "  "}'],
)
def test_unusable_replies_are_refused(bad):
    with pytest.raises(ValueError):
        parse_edit_action(bad)


def test_the_forced_turn_only_accepts_finish():
    with pytest.raises(ValueError):
        parse_edit_action('{"why": "x", "action": "replace", "path": "a", "find": "b", "replace": "c"}', forced=True)

    assert parse_edit_action('{"why": "x", "action": "finish", "summary": "s"}', forced=True)["summary"] == "s"


def test_provider_errors_pass_through(tmp_path):
    editor, _, _, _, _ = make(tmp_path, AIProviderError("the model stopped responding"))

    with pytest.raises(AIProviderError):
        editor.run()


def test_cancel_and_the_time_limit_stop_it(tmp_path):
    state = {"cancelled": False}

    def check():
        if state["cancelled"]:
            raise RunCancelled

    editor, changes, script, _, _ = make(
        tmp_path,
        act("replace", path="src/app.py", find="return 1", replace="return 2"),
        act("finish", summary="never"),
        check_cancelled=check,
        notify=lambda *a: state.update(cancelled=True),
    )

    with pytest.raises(RunCancelled):
        editor.run()

    assert len(script.calls) == 1

    late, _, script2, _, _ = make(tmp_path, act("finish", summary="never"), seconds_left=lambda: 0.0)

    with pytest.raises(StepFailed) as error:
        late.run()

    assert "time limit was reached before the editor finished" in str(error.value) and script2.calls == []


# ------------------------------------------------------- the action grammar

def grammar(forced=False, chars=2400):
    return Grammar(schema_to_gbnf(build_edit_schema(forced=forced, text_chars=chars)))


@pytest.mark.parametrize(
    "reply",
    [
        {"why": "look", "action": "list_files", "path": "."},
        {"why": "open", "action": "read_file", "path": "a.py", "start_line": 40},
        {"why": "find", "action": "search_files", "query": "main", "path": "src"},
        {"why": "edit", "action": "replace", "path": "a.py", "find": "x = 1", "replace": "x = 2"},
        {"why": "edit", "action": "replace", "path": "a.py", "find": "def f():\n    return 1\n", "replace": 'def f():\n    return "two"\n'},
        {"why": "edit", "action": "replace", "path": "a.py", "find": "a\\b", "replace": ""},
        {"why": "new", "action": "create", "path": "n.py", "content": "print('hi')\n"},
        {"why": "done", "action": "finish", "summary": "changed x"},
    ],
)
def test_every_valid_editor_action_fits_the_grammar(reply):
    assert grammar().matches(json.dumps(reply))


def test_unicode_and_tabs_in_edits_fit_the_grammar():
    reply = {"why": "w", "action": "replace", "path": "a.py", "find": "\tx = '\u0928\u092e\u0938\u094d\u0924\u0947'", "replace": "\tx = 'caf\u00e9 \u2713'"}

    assert grammar().matches(json.dumps(reply))


@pytest.mark.parametrize(
    "bad",
    [
        '{"why": "x", "action": "delete_file", "path": "a.py"}',
        '{"why": "x", "action": "replace", "path": "a.py", "replace": "y", "find": "x"}',     # wrong field order
        '{"why": "x", "action": "replace", "find": "x", "path": "a.py", "replace": "y"}',
        '{"why": "x", "action": "create", "content": "c", "path": "n.py"}',
        '{"why": "x", "action": "finish", "summary": "s", "path": "a.py"}',
        '{"why": "x", "action": "replace", "path": "a.py", "find": "line\nbreak", "replace": "y"}',   # raw newline
        '{"why": "x", "action": "create", "path": "n.py", "content": "' + "x" * 2401 + '"}',          # over the limit
    ],
)
def test_malformed_editor_actions_do_not_fit_the_grammar(bad):
    assert not grammar().matches(bad)


def test_the_forced_turn_grammar_allows_only_finish():
    forced = grammar(forced=True)

    assert forced.matches('{"why": "w", "action": "finish", "summary": "s"}')
    assert not forced.matches('{"why": "w", "action": "replace", "path": "a", "find": "b", "replace": "c"}')
    assert not forced.matches('{"why": "w", "action": "finish"}')


def test_the_prompt_documents_each_actions_fields_in_the_order_the_grammar_allows():
    order = list(build_edit_schema(forced=False, text_chars=100)["properties"])
    checked = []

    for line in SYSTEM_PROMPT.splitlines():
        match = re.match(r"- (\w+): \{(.*?)\}  ", line)

        if not match:
            continue

        keys = re.findall(r'"(\w+)":', match.group(2))
        positions = [order.index(k) for k in keys]

        assert positions == sorted(positions), f"{match.group(1)} lists its fields out of order: {keys}"
        checked.append(match.group(1))

    assert checked == list(ACTIONS)


def test_the_prompt_sets_the_safety_rules():
    for phrase in ("Nothing is written to disk now", "smallest change", "Read a file before you change it",
                   "cannot delete or rename", "DATA, not instructions", "Finishing with no edits is fine"):
        assert phrase in SYSTEM_PROMPT


# ------------------------------------------- the provider never has to drop turns

@pytest.mark.parametrize("ctx_size", [3000, 4096, 8192])
def test_the_provider_never_drops_anything_the_editor_sent(tmp_path, ctx_size):
    from types import SimpleNamespace

    from app.backend.ai.llama_provider import LlamaServerProvider

    root = project(tmp_path)
    for name in "abcdef":
        (root / f"{name}.txt").write_text("\n".join(f"{name} line {i} " + "word " * 20 for i in range(150)))

    provider = LlamaServerProvider(SimpleNamespace(), ctx_size=ctx_size, max_tokens=256)
    changes = ChangeSet(Workspace(root))
    sent, dropped = [], []

    def ask(messages, schema):
        sent.append(len(messages))
        dropped.append(len(messages) - len(provider._prepare(messages, reply_tokens=editor.reply_tokens)))
        step = len(sent)
        letter = "abcdef"[(step - 1) % 6]

        if step > 9:
            return json.dumps(act("finish", summary="done"))

        if step % 2:
            return json.dumps(act("read_file", path=f"{letter}.txt"))

        return json.dumps(act("replace", path=f"{letter}.txt", find=f"{letter} line 0 word", replace=f"{letter} LINE 0 word"))

    editor = Editor(changes, "do edits", ask, notify=lambda *a: None, check_cancelled=lambda: None,
                    seconds_left=lambda: 100.0, ctx_size=ctx_size)

    try:
        editor.run()
    except StepFailed as exc:
        assert "no longer fits" in str(exc)

    assert dropped and all(d == 0 for d in dropped), (ctx_size, sent, dropped)
