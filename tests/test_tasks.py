import threading
from types import SimpleNamespace

import pytest

from app.backend.agents import tasks
from app.backend.agents.events import ListSink
from app.backend.agents.supervisor import Supervisor
from app.backend.ai.base import AIProviderError


def ready_status(**extra):
    status = {
        "provider": "llama",
        "state": "ready",
        "detail": None,
        "model": "qwen.gguf",
        "models": [{"id": "qwen.gguf", "name": "qwen", "size": 1}, {"id": "b.gguf", "name": "b", "size": 1}],
        "can_switch": True,
        "setup": None,
    }
    status.update(extra)
    return status


class FakeService:
    """Stands in for the AI: replies with the given pieces of text."""

    def __init__(self, pieces=("I am ", "ready."), error=None, on_piece=None):
        self.pieces = pieces
        self.error = error
        self.on_piece = on_piece
        self.closed = False

    def stream_reply(self, messages):
        service = self

        def generator():
            try:
                for index, piece in enumerate(service.pieces):
                    if service.on_piece:
                        service.on_piece(index)
                    yield piece
                if service.error:
                    raise service.error
            finally:
                service.closed = True

        return generator()


def run_self_check(monkeypatch, status, service=None, cancel=None):
    runtime = SimpleNamespace(status=lambda: status, service=service or FakeService())
    monkeypatch.setattr(tasks, "get_runtime", lambda: runtime)

    sink = ListSink()
    result = Supervisor(sink, cancel or threading.Event(), tasks.SELF_CHECK.limits).run(
        tasks.SELF_CHECK.build({}), title="Self-check"
    )

    return result, sink.events


def titles(events, type_):
    return [e.title for e in events if e.type == type_]


# ------------------------------------------------------------- registry

def test_the_registry_knows_the_self_check():
    assert tasks.get_task("self_check") is tasks.SELF_CHECK
    assert tasks.get_task("nope") is None
    assert [t.kind for t in tasks.list_tasks()] == ["self_check", "ask_folder", "edit_files"]
    assert tasks.SELF_CHECK.title and tasks.SELF_CHECK.description


def test_each_build_gives_fresh_steps_within_the_limits():
    first, second = tasks.SELF_CHECK.build({}), tasks.SELF_CHECK.build({})

    assert first is not second
    assert len(first) <= tasks.SELF_CHECK.limits.max_steps
    assert [s.name for s in first] == ["engine", "models", "ask", "report"]


# ---------------------------------------------------------------- success

def test_a_healthy_engine_passes_every_check(monkeypatch):
    result, events = run_self_check(monkeypatch, ready_status())

    assert result.status == "completed"
    assert result.summary == "4 of 4 steps finished"
    assert titles(events, "step_started") == [
        "Step 1/4: Check the AI engine",
        "Step 2/4: List the available models",
        "Step 3/4: Ask the model a test question",
        "Step 4/4: Summarise the results",
    ]

    details = {e.title: e.detail for e in events if e.type == "step_finished"}

    assert details["Check the AI engine: done"] == "llama.cpp with qwen.gguf (ready)"
    assert details["List the available models: done"] == "2 models available: qwen, b"
    assert "Reply: I am ready." in details["Ask the model a test question: done"]
    assert "First text after" in details["Ask the model a test question: done"]
    assert details["Summarise the results: done"].startswith("All checks passed.")


def test_the_model_question_runs_as_the_reader_role(monkeypatch):
    _, events = run_self_check(monkeypatch, ready_status())

    ask = [e for e in events if e.type == "step_started" and "test question" in e.title][0]

    assert ask.role == "reader"


def test_echo_mode_is_called_out_but_does_not_fail(monkeypatch):
    status = ready_status(provider="echo", model=None, detail="Echo mode (no AI model).")

    result, events = run_self_check(monkeypatch, status)

    assert result.status == "completed"
    assert titles(events, "warning") == ["Echo mode: no AI model is loaded"]


def test_a_model_that_is_still_loading_is_noted(monkeypatch):
    result, events = run_self_check(monkeypatch, ready_status(state="loading"))

    assert result.status == "completed"
    assert "qwen.gguf is still loading" in titles(events, "info")


def test_a_long_model_list_is_shortened(monkeypatch):
    models = [{"id": f"m{i}.gguf", "name": f"m{i}", "size": 1} for i in range(8)]

    _, events = run_self_check(monkeypatch, ready_status(models=models))
    detail = [e.detail for e in events if e.type == "step_finished" and "models" in e.title][0]

    assert detail == "8 models available: m0, m1, m2, m3, m4 and 3 more"


# ---------------------------------------------------------------- failure

def test_an_unavailable_engine_stops_the_run_at_step_one(monkeypatch):
    status = ready_status(provider="unavailable", state="error", detail="no llama-server found")

    result, events = run_self_check(monkeypatch, status)

    assert result.status == "failed"
    assert result.error == "Check the AI engine: no llama-server found"
    assert titles(events, "step_started") == ["Step 1/4: Check the AI engine"]


def test_an_engine_that_failed_to_start_is_reported(monkeypatch):
    status = ready_status(state="error", detail="out of memory")

    result, _ = run_self_check(monkeypatch, status)

    assert result.status == "failed" and "out of memory" in result.error


def test_no_models_is_skipped_not_fatal(monkeypatch):
    result, events = run_self_check(monkeypatch, ready_status(models=[]))

    assert result.status == "completed"
    assert result.summary == "3 of 4 steps finished, 1 skipped"
    assert titles(events, "step_skipped") == [
        "List the available models: no .gguf models found in the models folder (carrying on)"
    ]


def test_an_ai_error_fails_the_question_step(monkeypatch):
    service = FakeService(pieces=(), error=AIProviderError("llama-server stopped responding"))

    result, events = run_self_check(monkeypatch, ready_status(), service)

    assert result.status == "failed"
    assert result.error == "Ask the model a test question: llama-server stopped responding"
    assert "Step 4/4: Summarise the results" not in titles(events, "step_started")
    assert service.closed


def test_an_empty_reply_fails(monkeypatch):
    result, _ = run_self_check(monkeypatch, ready_status(), FakeService(pieces=("  ", "\n")))

    assert result.status == "failed"
    assert result.error == "Ask the model a test question: the model sent an empty reply"


# ------------------------------------------------------------ cancellation

def test_cancel_while_the_model_is_replying_stops_and_closes_the_stream(monkeypatch):
    cancel = threading.Event()
    service = FakeService(
        pieces=("one ", "two ", "three ", "four "),
        on_piece=lambda index: cancel.set() if index == 1 else None,
    )

    result, events = run_self_check(monkeypatch, ready_status(), service, cancel)

    assert result.status == "cancelled"
    assert service.closed                                   # the AI call was released
    assert "Step 4/4: Summarise the results" not in titles(events, "step_started")
    assert events[-1].type == "run_finished" and "cancelled" in events[-1].title


def test_cancel_before_the_run_starts_does_nothing_at_all(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    service = FakeService()

    result, events = run_self_check(monkeypatch, ready_status(), service, cancel)

    assert result.status == "cancelled"
    assert not service.closed and titles(events, "step_started") == []


# ===================================================== ask about a folder

import json
import os
from pathlib import Path

from app.backend.agents.tasks import ParamError, allowed_roots, validate_ask_folder


def make_project(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    print('hello world')\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo app\n", encoding="utf-8")
    return root


def good_params(root, **extra):
    return {"folder": str(root), "question": "What does the app print?", **extra}


# ------------------------------------------------------------ validation

def test_valid_parameters_are_normalised(tmp_path):
    root = make_project(tmp_path)

    result = validate_ask_folder(
        {"folder": f'  "{root}"  ', "question": "  What   does\nit   do?  "}, env={}
    )

    assert result == {"folder": str(root.resolve()), "question": "What does it do?"}


@pytest.mark.parametrize(
    "params, message",
    [
        ({"question": "q"}, "Enter the folder"),
        ({"folder": "   ", "question": "q"}, "Enter the folder"),
        ({"folder": "x"}, "Enter a question"),
        ({"folder": "x", "question": "   "}, "Enter a question"),
        ({"folder": "x", "question": "q" * 1001}, "too long"),
    ],
)
def test_missing_or_oversized_input_is_explained(params, message):
    with pytest.raises(ParamError) as error:
        validate_ask_folder(params, env={})

    assert message in str(error.value)


def test_the_folder_must_be_a_full_path_to_a_real_folder(tmp_path):
    root = make_project(tmp_path)

    for params, message in (
        ({"folder": "project", "question": "q"}, "full path"),
        ({"folder": str(tmp_path / "missing"), "question": "q"}, "does not exist"),
        ({"folder": str(root / "README.md"), "question": "q"}, "a file, not a folder"),
    ):
        with pytest.raises(ParamError) as error:
            validate_ask_folder(params, env={})

        assert message in str(error.value)


def test_non_text_parameters_do_not_crash_validation(tmp_path):
    with pytest.raises(ParamError):
        validate_ask_folder({"folder": 5, "question": ["a"]}, env={})


def test_allowed_roots_come_from_the_environment(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()

    assert allowed_roots({}) == []
    assert allowed_roots({"ORION_AGENT_ROOTS": ""}) == []
    assert allowed_roots({"ORION_AGENT_ROOTS": f"{one}{os.pathsep}{os.pathsep}{two}"}) == [one.resolve(), two.resolve()]


def test_allowed_roots_restrict_which_folders_can_be_read(tmp_path):
    inside = tmp_path / "allowed" / "proj"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    env = {"ORION_AGENT_ROOTS": str(tmp_path / "allowed")}

    assert validate_ask_folder({"folder": str(inside), "question": "q"}, env=env)["folder"] == str(inside.resolve())
    assert validate_ask_folder({"folder": str(tmp_path / "allowed"), "question": "q"}, env=env)

    with pytest.raises(ParamError) as error:
        validate_ask_folder({"folder": str(outside), "question": "q"}, env=env)

    assert "ORION_AGENT_ROOTS" in str(error.value)


def test_a_symlink_cannot_sneak_past_the_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    try:
        os.symlink(outside, allowed / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")

    with pytest.raises(ParamError):
        validate_ask_folder({"folder": str(allowed / "link"), "question": "q"}, env={"ORION_AGENT_ROOTS": str(allowed)})


def test_orions_own_data_folder_is_off_limits(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "inner").mkdir(parents=True)
    monkeypatch.setattr(tasks, "DATA_DIR", data)

    for folder in (data, data / "inner"):
        with pytest.raises(ParamError) as error:
            validate_ask_folder({"folder": str(folder), "question": "q"}, env={})

        assert "off limits" in str(error.value)


def test_run_titles_show_the_question():
    task = tasks.ASK_FOLDER

    assert task.title_for({"question": "What is this?"}) == "Ask: What is this?"
    assert task.title_for({"question": "x" * 100}) == "Ask: " + "x" * 57 + "..."
    assert tasks.SELF_CHECK.title_for({}) == "Self-check"


def test_the_folder_task_describes_its_form():
    assert [(f.name, f.kind) for f in tasks.ASK_FOLDER.fields] == [("folder", "text"), ("question", "textarea")]
    assert tasks.SELF_CHECK.fields == ()
    assert tasks.SELF_CHECK.validate({"anything": 1}) == {}


# --------------------------------------------------------------- running it

class ScriptedService:
    """Stands in for the AI service: plays back prepared structured replies."""

    def __init__(self, *replies, after_call=None):
        self.replies = list(replies)
        self.after_call = after_call
        self.calls = []

    def generate_structured(self, messages, schema, *, max_tokens=None):
        self.calls.append({"messages": [dict(m) for m in messages], "schema": schema, "max_tokens": max_tokens})
        reply = self.replies.pop(0)

        if self.after_call:
            self.after_call(len(self.calls))

        if isinstance(reply, Exception):
            raise reply

        return reply if isinstance(reply, str) else json.dumps(reply)


def act(action, why="because", **args):
    return {"why": why, "action": action, **args}


def run_ask_folder(monkeypatch, root, service, status=None, cancel=None, params=None):
    runtime = SimpleNamespace(
        status=lambda: status or ready_status(),
        service=service,
        settings=SimpleNamespace(ctx_size=8192),
    )
    monkeypatch.setattr(tasks, "get_runtime", lambda: runtime)

    sink = ListSink()
    cancel = cancel or threading.Event()
    result = Supervisor(sink, cancel, tasks.ASK_FOLDER.limits).run(
        tasks.ASK_FOLDER.build(params or good_params(root)), title="Ask: test"
    )

    return result, sink.events


def test_the_folder_task_has_the_expected_steps(tmp_path):
    steps = tasks.ASK_FOLDER.build(good_params(make_project(tmp_path)))

    assert [(s.name, s.role) for s in steps] == [
        ("model", "supervisor"), ("open", "supervisor"), ("read", "reader"), ("answer", "supervisor"),
    ]


def test_a_question_is_answered_from_the_files(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    service = ScriptedService(
        act("list_files", "see the layout", path="."),
        act("read_file", "open the app", path="src/app.py"),
        act("answer", "done", answer="It prints **hello world** (`src/app.py:2`)."),
    )

    result, events = run_ask_folder(monkeypatch, root, service)

    assert result.status == "completed"
    # The run's summary is the answer, followed by what it is based on (written by code).
    assert result.summary == (
        "It prints **hello world** (`src/app.py:2`).\n\n**Files read:** `src/app.py` (all 2 lines)"
    )
    assert [e.title for e in events if e.type == "tool_call"] == ["list_files .", "read_file src/app.py"]
    assert all(e.role == "reader" for e in events if e.type == "tool_call")

    finished = {e.title: e.detail for e in events if e.type == "step_finished"}
    assert finished["Check the AI model: done"] == "Using qwen.gguf."
    assert "Read-only access to" in finished["Open the folder (read-only): done"]
    assert "README.md" in finished["Open the folder (read-only): done"]
    assert finished["Explore the files and find the answer: done"] == "Used 2 tool calls."
    assert finished["Write up the answer: done"].startswith("Answer ready (")

    assert all(call["max_tokens"] == 1200 for call in service.calls)      # min(1200, 8192 // 4)
    assert "hello world" in service.calls[2]["messages"][-1]["content"]


def test_the_models_view_of_the_folder_hides_orions_data(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "orion.db").write_text("password hashes")
    monkeypatch.setattr(tasks, "DATA_DIR", root / "data")
    service = ScriptedService(
        act("read_file", path="data/orion.db"),
        act("search_files", query="password"),
        act("answer", answer="nothing"),
    )

    _, events = run_ask_folder(monkeypatch, root, service)
    shown = " ".join(str(c["messages"]) for c in service.calls)

    assert "password hashes" not in shown
    assert "off limits" in service.calls[1]["messages"][-1]["content"]
    assert "data/" not in service.calls[0]["messages"][1]["content"]


def test_echo_mode_fails_immediately_with_a_clear_reason(tmp_path, monkeypatch):
    service = ScriptedService()
    status = ready_status(provider="echo", model=None)

    result, events = run_ask_folder(monkeypatch, make_project(tmp_path), service, status)

    assert result.status == "failed"
    assert "echo mode" in result.error and result.error.startswith("Check the AI model:")
    assert titles(events, "step_started") == ["Step 1/4: Check the AI model"]
    assert service.calls == []


def test_an_engine_that_is_down_fails_immediately(tmp_path, monkeypatch):
    status = ready_status(provider="unavailable", state="error", detail="no llama-server found")

    result, _ = run_ask_folder(monkeypatch, make_project(tmp_path), ScriptedService(), status)

    assert result.status == "failed" and result.error == "Check the AI model: no llama-server found"


def test_a_model_still_loading_is_noted_but_does_not_stop_the_run(tmp_path, monkeypatch):
    service = ScriptedService(act("answer", answer="ok"))

    result, events = run_ask_folder(monkeypatch, make_project(tmp_path), service, ready_status(state="loading"))

    assert result.status == "completed"
    assert "qwen.gguf is still loading" in titles(events, "info")


def test_an_ai_error_midway_fails_the_run_with_its_message(tmp_path, monkeypatch):
    service = ScriptedService(act("list_files", path="."), AIProviderError("llama-server stopped responding"))

    result, events = run_ask_folder(monkeypatch, make_project(tmp_path), service)

    assert result.status == "failed"
    assert result.error == "Explore the files and find the answer: llama-server stopped responding"
    assert "Step 4/4: Write up the answer" not in titles(events, "step_started")
    assert len(titles(events, "tool_call")) == 1                       # what it did is still logged


def test_a_folder_that_vanished_since_validation_fails_cleanly(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    params = good_params(root)
    import shutil
    shutil.rmtree(root)

    result, _ = run_ask_folder(monkeypatch, root, ScriptedService(), params=params)

    assert result.status == "failed" and "not a folder" in result.error


def test_cancelling_between_model_turns_stops_the_run(tmp_path, monkeypatch):
    cancel = threading.Event()
    service = ScriptedService(
        act("list_files", path="."),
        act("list_files", path="src"),
        act("answer", answer="never"),
        after_call=lambda n: cancel.set() if n == 2 else None,
    )

    result, events = run_ask_folder(monkeypatch, make_project(tmp_path), service, cancel=cancel)

    assert result.status == "cancelled"
    assert len(titles(events, "tool_call")) == 1                       # the second action was not carried out
    assert len(service.calls) == 2


def test_a_model_that_never_answers_still_ends(tmp_path, monkeypatch):
    same = act("list_files", path=".")
    # Repeats force a final answer; ignoring that gets one repair, then it gives up.
    service = ScriptedService(same, same, same, same, same, same)

    result, _ = run_ask_folder(monkeypatch, make_project(tmp_path), service)

    assert result.status == "failed"
    assert len(service.calls) == 6
    assert "did not give a usable action" in result.error


def test_the_fallback_is_announced_once_when_output_is_not_enforced(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    service = ScriptedService(
        act("list_files", path="."), act("read_file", path="src/app.py"), act("answer", answer="ok"),
    )
    runtime = SimpleNamespace(
        status=lambda: ready_status(),
        service=service,
        settings=SimpleNamespace(ctx_size=8192),
        provider=SimpleNamespace(structured_fallback=True),
    )
    monkeypatch.setattr(tasks, "get_runtime", lambda: runtime)
    sink = ListSink()

    result = Supervisor(sink, threading.Event(), tasks.ASK_FOLDER.limits).run(
        tasks.ASK_FOLDER.build(good_params(root)), title="t"
    )

    assert result.status == "completed"
    assert titles(sink.events, "warning") == ["Output format is not enforced for this model"]


def test_no_warning_when_the_output_is_enforced(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    runtime = SimpleNamespace(
        status=lambda: ready_status(),
        service=ScriptedService(act("answer", answer="ok")),
        settings=SimpleNamespace(ctx_size=8192),
        provider=SimpleNamespace(structured_fallback=False),
    )
    monkeypatch.setattr(tasks, "get_runtime", lambda: runtime)
    sink = ListSink()

    Supervisor(sink, threading.Event(), tasks.ASK_FOLDER.limits).run(tasks.ASK_FOLDER.build(good_params(root)), title="t")

    assert titles(sink.events, "warning") == []


# ===================================================================== edit files

import time

from app.backend.agents.supervisor import AcknowledgementRequired, ApprovalGate
from app.backend.agents.tasks import validate_edit_files

APP_TEXT = "import os\n\ndef main():\n    print('hello world')\n    return 1\n"


def edit_project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(APP_TEXT, newline="\n")
    (root / "README.md").write_bytes(b"# Demo\n")

    return root


def edit_params(root, **extra):
    return {"folder": str(root), "instruction": "Make main return 2", **extra}


# ------------------------------------------------------------ validation

def test_valid_edit_parameters_are_normalised(tmp_path):
    root = edit_project(tmp_path)

    result = validate_edit_files({"folder": f' "{root}" ', "instruction": "  Make   main\nreturn 2 "}, env={})

    assert result == {"folder": str(root.resolve()), "instruction": "Make main return 2"}


@pytest.mark.parametrize(
    "params, message",
    [
        ({"instruction": "x"}, "Enter the folder to change"),
        ({"folder": "x"}, "Describe the change"),
        ({"folder": "x", "instruction": "   "}, "Describe the change"),
        ({"folder": "x", "instruction": "q" * 2001}, "too long"),
    ],
)
def test_missing_or_oversized_edit_input_is_explained(params, message):
    with pytest.raises(ParamError) as error:
        validate_edit_files(params, env={})

    assert message in str(error.value)


def test_editing_shares_the_folder_rules(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(tasks, "DATA_DIR", data)

    for folder, message in (
        ("relative/dir", "full path"),
        (str(tmp_path / "missing"), "does not exist"),
        (str(root / "README.md"), "a file, not a folder"),
        (str(data), "off limits"),
    ):
        with pytest.raises(ParamError) as error:
            validate_edit_files({"folder": folder, "instruction": "x"}, env={})

        assert message in str(error.value)

    with pytest.raises(ParamError) as error:
        validate_edit_files({"folder": str(root), "instruction": "x"}, env={"ORION_AGENT_ROOTS": str(tmp_path / "other")})

    assert "ORION_AGENT_ROOTS" in str(error.value)


def test_a_whole_drive_cannot_be_edited(tmp_path):
    with pytest.raises(ParamError) as error:
        validate_edit_files({"folder": tmp_path.anchor, "instruction": "x"}, env={})

    assert "specific project folder" in str(error.value)


def test_edit_run_titles_and_form():
    task = tasks.EDIT_FILES

    assert task.title_for({"instruction": "Fix it"}) == "Edit: Fix it"
    assert task.title_for({"instruction": "x" * 100}) == "Edit: " + "x" * 57 + "..."
    assert [(f.name, f.kind) for f in task.fields] == [("folder", "text"), ("instruction", "textarea")]
    assert task.limits.max_seconds >= 3600                                  # waiting for you must not time out the run


def test_the_edit_task_has_the_expected_steps(tmp_path):
    steps = tasks.EDIT_FILES.build(edit_params(edit_project(tmp_path)))

    assert [(s.name, s.role) for s in steps] == [
        ("model", "supervisor"), ("open", "supervisor"), ("edit", "editor"),
        ("check", "security"), ("approve", "supervisor"), ("apply", "supervisor"),
    ]


# ------------------------------------------------------------ running it

def run_edit(monkeypatch, tmp_path, root, service, decision=None, cancel=None, before_decision=None,
             status=None, params=None, run_id="run-1", acknowledge=False):
    """Run the edit task for real; ``decision`` is given from another thread, like a click.

    If applying is refused because high-risk findings were not acknowledged, that is recorded in
    ``run_edit.blocked`` and the click falls back to rejecting so the run can end.
    """
    run_edit.blocked = []
    monkeypatch.setattr(tasks, "DATA_DIR", tmp_path / "data")
    runtime = SimpleNamespace(
        status=lambda: status or ready_status(), service=service, settings=SimpleNamespace(ctx_size=8192)
    )
    monkeypatch.setattr(tasks, "get_runtime", lambda: runtime)

    sink, statuses, gate = ListSink(), [], ApprovalGate()
    cancel = cancel or threading.Event()

    def click():
        for _ in range(300):
            if gate.waiting:
                if before_decision:
                    before_decision()

                if decision:
                    try:
                        gate.decide(decision, acknowledge)
                    except AcknowledgementRequired:
                        run_edit.blocked.append(True)
                        gate.decide("reject")

                return

            time.sleep(0.01)

    if decision or before_decision:
        threading.Thread(target=click, daemon=True).start()

    supervisor = Supervisor(
        sink, cancel, tasks.EDIT_FILES.limits, gate=gate, status_hook=statuses.append,
        state={"run_id": run_id},
    )
    result = supervisor.run(tasks.EDIT_FILES.build(params or edit_params(root)), title="Edit: test")

    return result, sink.events, statuses


def one_edit_script():
    return ScriptedService(
        act("read_file", "look first", path="src/app.py"),
        act("replace", "make the change", path="src/app.py", find="    return 1", replace="    return 2"),
        act("finish", "done", summary="main now returns 2."),
    )


def test_an_approved_edit_is_written_with_a_backup(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    result, events, statuses = run_edit(monkeypatch, tmp_path, root, one_edit_script(), decision="apply")

    assert result.status == "completed"
    assert (root / "src" / "app.py").read_text() == APP_TEXT.replace("return 1", "return 2")
    assert (tmp_path / "data" / "run_changes" / "run-1" / "manifest.json").exists()
    assert statuses == ["awaiting_approval", "running"]

    assert result.summary.startswith("**Applied 1 file change** (+1 \u22121): `src/app.py`.")
    assert "main now returns 2." in result.summary and "Undo changes" in result.summary

    proposal = [e for e in events if e.type == "proposal"][0]
    assert proposal.title == "Proposed changes: 1 file (+1 \u22121)"
    assert "--- a/src/app.py" in proposal.detail and "+    return 2" in proposal.detail
    assert proposal.role == "supervisor"
    assert [e.title for e in events if e.type == "tool_call"] == ["read_file src/app.py", "replace src/app.py"]
    assert all(e.role == "editor" for e in events if e.type == "tool_call")


def test_a_rejected_edit_changes_nothing(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    result, events, _ = run_edit(monkeypatch, tmp_path, root, one_edit_script(), decision="reject")

    assert result.status == "completed"
    assert (root / "src" / "app.py").read_text() == APP_TEXT
    assert not (tmp_path / "data" / "run_changes").exists()
    assert result.summary.startswith("You rejected the proposed changes; nothing was written.")
    assert "Rejected: nothing was changed." in [e.detail for e in events if e.type == "step_finished"]


def test_nothing_is_written_before_the_decision(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    seen = []

    run_edit(
        monkeypatch, tmp_path, root, one_edit_script(), decision="reject",
        before_decision=lambda: seen.append((root / "src" / "app.py").read_text()),
    )

    assert seen == [APP_TEXT]                                                # the file was untouched while the diff was on screen


def test_cancelling_while_the_diff_is_shown_changes_nothing(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    cancel = threading.Event()
    threading.Timer(0.4, cancel.set).start()

    result, _, statuses = run_edit(monkeypatch, tmp_path, root, one_edit_script(), cancel=cancel)

    assert result.status == "cancelled"
    assert (root / "src" / "app.py").read_text() == APP_TEXT
    assert not (tmp_path / "data" / "run_changes").exists()
    assert statuses[-1] == "running"


def test_no_edits_means_no_approval_is_asked_for(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    service = ScriptedService(act("finish", "cannot", summary="The request cannot be done with these files."))

    result, events, statuses = run_edit(monkeypatch, tmp_path, root, service)

    assert result.status == "completed" and statuses == []
    assert result.summary == "No changes were proposed.\n\nThe request cannot be done with these files."
    assert not [e for e in events if e.type == "proposal"]


def test_a_file_changed_while_waiting_is_never_overwritten(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    mine = "# the user kept working in this file\n"

    result, _, _ = run_edit(
        monkeypatch, tmp_path, root, one_edit_script(), decision="apply",
        before_decision=lambda: (root / "src" / "app.py").write_text(mine),
    )

    assert result.status == "failed"
    assert "src/app.py changed since the proposal" in result.error and "nothing was written" in result.error
    assert (root / "src" / "app.py").read_text() == mine


def test_several_files_are_applied_together_with_new_ones(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    service = ScriptedService(
        act("replace", path="src/app.py", find="    return 1", replace="    return 2"),
        act("create", path="src/util.py", content="def helper():\n    return 42\n"),
        act("replace", path="README.md", find="Demo", replace="Demo app"),
        act("finish", summary="three files"),
    )

    result, events, _ = run_edit(monkeypatch, tmp_path, root, service, decision="apply")

    assert result.status == "completed"
    assert (root / "src" / "util.py").read_text() == "def helper():\n    return 42\n"
    assert (root / "README.md").read_bytes() == b"# Demo app\n"
    assert "**Applied 3 file changes**" in result.summary
    assert [e.title for e in events if e.type == "proposal"][0].startswith("Proposed changes: 3 files")


def test_a_write_failure_is_reported_and_everything_is_put_back(tmp_path, monkeypatch):
    from app.backend.agents import changes as changes_module

    root = edit_project(tmp_path)
    service = ScriptedService(
        act("replace", path="src/app.py", find="    return 1", replace="    return 2"),
        act("replace", path="README.md", find="Demo", replace="Demo app"),
        act("finish", summary="two files"),
    )
    real = changes_module._atomic_write

    def flaky(path, data):
        if path.name == "README.md":
            raise OSError("disk full")

        real(path, data)

    monkeypatch.setattr(changes_module, "_atomic_write", flaky)

    result, _, _ = run_edit(monkeypatch, tmp_path, root, service, decision="apply")

    monkeypatch.setattr(changes_module, "_atomic_write", real)
    assert result.status == "failed" and "disk full" in result.error
    assert (root / "src" / "app.py").read_text() == APP_TEXT and (root / "README.md").read_bytes() == b"# Demo\n"


def test_editing_in_echo_mode_fails_before_anything_else(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    service = ScriptedService()

    result, events, _ = run_edit(monkeypatch, tmp_path, root, service, status=ready_status(provider="echo", model=None))

    assert result.status == "failed" and "echo mode" in result.error
    assert titles(events, "step_started") == ["Step 1/6: Check the AI model"] and service.calls == []


def test_the_editor_cannot_touch_secrets_or_orions_data_through_the_task(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    (root / ".env").write_text("KEY=1\n")
    (tmp_path / "data").mkdir()
    service = ScriptedService(
        act("replace", path=".env", find="KEY=1", replace="KEY=2"),
        act("create", path=".git/hooks/pre-commit", content="x\n"),
        act("finish", summary="refused"),
    )

    result, events, _ = run_edit(monkeypatch, tmp_path, root, service)

    assert result.status == "completed" and result.summary.startswith("No changes were proposed.")
    assert (root / ".env").read_text() == "KEY=1\n"
    assert [e.title for e in events if e.type == "tool_call"] == ["replace .env (error)", "create .git/hooks/pre-commit (error)"]


def test_the_apply_step_needs_a_run_id_for_its_backup(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    result, _, _ = run_edit(monkeypatch, tmp_path, root, one_edit_script(), decision="apply", run_id="")

    assert result.status == "failed" and "no id" in result.error
    assert (root / "src" / "app.py").read_text() == APP_TEXT


# -------------------------------------------------------- the security step

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def risky_script(line):
    return ScriptedService(
        act("replace", path="src/app.py", find="    return 1", replace=f"    return 1\n{line}"),
        act("finish", summary="added a line"),
    )


def check_event(events):
    return [e for e in events if e.type == "check"]


def test_a_clean_change_is_checked_and_reported_as_such(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    result, events, _ = run_edit(monkeypatch, tmp_path, root, one_edit_script(), decision="apply")

    event = check_event(events)[0]

    assert result.status == "completed" and event.role == "security"
    assert event.title == "Security checks: nothing unusual found"
    assert json.loads(event.detail) == {"findings": []}
    assert "Step 4/6: Run security checks (plain code, no AI)" in titles(events, "step_started")
    assert "Checked 1 file: nothing unusual found." in [e.detail for e in events if e.type == "step_finished"]
    assert "Security checks" not in result.summary                         # nothing to report in the summary


def test_checks_run_before_the_diff_is_shown(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    _, events, _ = run_edit(monkeypatch, tmp_path, root, one_edit_script(), decision="reject")
    kinds = [e.type for e in events if e.type in ("check", "proposal")]

    assert kinds == ["check", "proposal"]


def test_a_secret_is_reported_but_never_stored_in_full(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    _, events, _ = run_edit(monkeypatch, tmp_path, root, risky_script(f'    key = "{AWS_KEY}"'), decision="reject")

    event = check_event(events)[0]
    finding = json.loads(event.detail)["findings"][0]

    assert event.title == "Security checks: 1 high-risk finding"
    assert finding["code"] == "aws_key" and finding["severity"] == "high" and finding["line"] == 6
    assert finding["path"] == "src/app.py" and "AKIA****" in finding["excerpt"]
    assert AWS_KEY not in event.detail and AWS_KEY not in event.title


def test_a_high_risk_change_cannot_be_applied_without_acknowledging_it(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    result, _, _ = run_edit(monkeypatch, tmp_path, root, risky_script(f'    key = "{AWS_KEY}"'), decision="apply")

    assert run_edit.blocked == [True]                                       # "Apply" alone was refused
    assert (root / "src" / "app.py").read_text() == APP_TEXT                # nothing was written
    assert result.summary.startswith("You rejected")


def test_an_acknowledged_high_risk_change_is_applied_and_the_summary_says_so(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    result, _, _ = run_edit(
        monkeypatch, tmp_path, root, risky_script(f'    key = "{AWS_KEY}"'), decision="apply", acknowledge=True
    )

    assert run_edit.blocked == [] and result.status == "completed"
    assert AWS_KEY in (root / "src" / "app.py").read_text()                 # the user chose to
    assert "Security checks: 1 high-risk finding." in result.summary
    assert "You confirmed the high-risk findings before applying." in result.summary


def test_rejecting_a_high_risk_change_needs_no_acknowledgement(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    result, _, _ = run_edit(monkeypatch, tmp_path, root, risky_script(f'    key = "{AWS_KEY}"'), decision="reject")

    assert run_edit.blocked == [] and result.summary.startswith("You rejected")


def test_warnings_alone_do_not_need_acknowledgement(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    service = ScriptedService(
        act("create", path="deploy.ps1", content="Write-Host hello\n"),
        act("finish", summary="added a script"),
    )

    result, events, _ = run_edit(monkeypatch, tmp_path, root, service, decision="apply")

    assert run_edit.blocked == [] and result.status == "completed"
    assert (root / "deploy.ps1").exists()
    assert check_event(events)[0].title == "Security checks: 1 warning"
    assert "Security checks: 1 warning." in result.summary and "You confirmed" not in result.summary


def test_a_broken_checker_fails_closed(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    def broken(changes):
        raise RuntimeError("the checker crashed")

    monkeypatch.setattr(tasks, "run_checks", broken)

    result, events, statuses = run_edit(monkeypatch, tmp_path, root, one_edit_script(), decision="apply")

    assert result.status == "failed" and "the checker crashed" in result.error
    assert statuses == [] and not [e for e in events if e.type == "proposal"]     # never even asked
    assert (root / "src" / "app.py").read_text() == APP_TEXT


def test_with_no_changes_there_is_nothing_to_check(tmp_path, monkeypatch):
    root = edit_project(tmp_path)
    service = ScriptedService(act("finish", summary="nothing to do"))

    _, events, _ = run_edit(monkeypatch, tmp_path, root, service)

    assert not check_event(events)
    assert "Nothing to check." in [e.detail for e in events if e.type == "step_finished"]


# --------------------------------------- secrets are masked wherever they are logged

def test_the_logged_diff_hides_a_secret_but_the_file_gets_the_real_text(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    result, events, _ = run_edit(
        monkeypatch, tmp_path, root, risky_script(f'    key = "{AWS_KEY}"'), decision="apply", acknowledge=True
    )

    proposal = [e for e in events if e.type == "proposal"][0]

    assert proposal.detail.startswith("# Secret-looking values are hidden in this view (shown as ****).")
    assert '+    key = "AKIA****"' in proposal.detail and AWS_KEY not in proposal.detail
    assert f'key = "{AWS_KEY}"' in (root / "src" / "app.py").read_text()          # what is written is real


def test_a_clean_diff_has_no_masking_note(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    _, events, _ = run_edit(monkeypatch, tmp_path, root, one_edit_script(), decision="reject")

    assert not [e for e in events if e.type == "proposal"][0].detail.startswith("#")


def test_tool_previews_in_the_log_hide_secrets_for_the_editor(tmp_path, monkeypatch):
    root = edit_project(tmp_path)

    _, events, _ = run_edit(monkeypatch, tmp_path, root, risky_script(f'    key = "{AWS_KEY}"'), decision="reject")

    everything = " ".join(f"{e.title} {e.detail}" for e in events)

    assert AWS_KEY not in everything and "AKIA****" in everything


def test_tool_previews_in_the_log_hide_secrets_for_the_reader(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    (root / "settings.py").write_text(f'API = "{AWS_KEY}"\nPASSWORD = "hunter2hunter2X9"\n')
    service = ScriptedService(
        act("read_file", path="settings.py"),
        act("answer", answer="It holds credentials."),
    )

    result, events, = run_ask_folder(monkeypatch, root, service)

    everything = " ".join(f"{e.title} {e.detail}" for e in events)

    assert AWS_KEY not in everything and "hunter2hunter2X9" not in everything
    assert 'API = "AKIA****"' in everything and 'PASSWORD = "****"' in everything
    assert result.status == "completed"

