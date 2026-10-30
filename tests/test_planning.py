"""Stage-one safety tests, including the real planner/editor/supervisor pipeline."""
import json
import threading
from types import SimpleNamespace

import pytest

from app.backend.agents import tasks
from app.backend.agents.changes import ChangeSet, ScopeViolation
from app.backend.agents.planner import Planner
from app.backend.agents.supervisor import ApprovalGate, Supervisor
from app.backend.agents.workspace import Workspace


def plan_action(**extra):
    return dict(why="inspected", action="finish", summary="Change app.py to return 2",
                files=["app.py"], verification="Proposed pytest; not executed",
                out_of_scope="No new dependencies or unrelated changes", **extra)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    (tmp_path / "other.py").write_text("untouched = True\n")
    return Workspace(tmp_path)


@pytest.mark.parametrize("path", ["other.py", "new.py"])
def test_unapproved_existing_and_new_files_are_blocked(project, path):
    changes = ChangeSet(project, allowed_files=("app.py",))
    with pytest.raises(ScopeViolation):
        changes.create(path, "bad = True\n")
    with pytest.raises(ScopeViolation):
        changes.replace(path, "untouched", "bad")
    assert not changes


def test_empty_scope_denies_every_write(project):
    with pytest.raises(ScopeViolation):
        ChangeSet(project, allowed_files=()).replace("app.py", "1", "2")


def test_scope_is_checked_again_at_apply(project, tmp_path):
    changes = ChangeSet(project, allowed_files=("app.py",))
    changes.replace("app.py", "1", "2")
    changes.allowed_files = frozenset()
    with pytest.raises(ScopeViolation):
        changes.apply(tmp_path / "backup")
    assert (project.root / "app.py").read_text() == "value = 1\n"


@pytest.mark.parametrize("bad", ["../escape.py", "/tmp/out.py", "*.py", ".env", ".git/config"])
def test_planner_rejects_unsafe_scope(project, bad):
    planner = Planner(ChangeSet(project), "Change value", lambda *_: "",
                      notify=lambda *_: None, check_cancelled=lambda: None, seconds_left=lambda: 60)
    action = plan_action()
    action["files"] = [bad]
    with pytest.raises(ValueError):
        planner.parse(json.dumps(action), False)


def test_planner_rejects_edit_actions(project):
    planner = Planner(ChangeSet(project), "Change value", lambda *_: "",
                      notify=lambda *_: None, check_cancelled=lambda: None, seconds_left=lambda: 60)
    with pytest.raises(ValueError):
        planner.parse(json.dumps(dict(action="create", path="bad.py", content="x")), False)


@pytest.mark.parametrize("decisions, expected, written", [
    (["reject"], "failed", False),
    (["apply", "reject"], "completed", False),
    (["apply", "apply"], "completed", True),
])
def test_real_pipeline_requires_two_independent_approvals(project, monkeypatch, tmp_path,
                                                         decisions, expected, written):
    replies = [dict(action="read_file", path="app.py", why="inspect"), plan_action(),
               dict(action="replace", path="app.py", find="value = 1", replace="value = 2", why="edit"),
               dict(action="finish", summary="Updated value", why="done")]
    class Service:
        def generate_structured(self, messages, schema, **kwargs):
            return json.dumps(replies.pop(0))
    runtime = SimpleNamespace(settings=SimpleNamespace(ctx_size=8192), service=Service(),
                              status=lambda: {"provider": "llama", "state": "ready", "model": "test.gguf"})
    monkeypatch.setattr(tasks, "get_runtime", lambda: runtime)
    monkeypatch.setattr(tasks, "DATA_DIR", tmp_path / "orion-data")
    events = []
    sink = SimpleNamespace(emit=events.append)
    gate = ApprovalGate()
    pending = iter(decisions)
    observations = []
    def status(value):
        if value == "awaiting_approval":
            observations.append((project.root / "app.py").read_text())
            gate.decide(next(pending))
    supervisor = Supervisor(sink, threading.Event(), gate=gate, status_hook=status,
                            state={"run_id": "stage-one"})
    result = supervisor.run(tasks.EDIT_FILES.build({"folder": str(project.root), "instruction": "Set value to 2"}))
    assert result.status == expected
    assert (project.root / "app.py").read_text() == ("value = 2\n" if written else "value = 1\n")
    assert observations == ["value = 1\n"] * len(decisions)
    assert (project.root / "other.py").read_text() == "untouched = True\n"
    if len(decisions) == 1:
        assert not any(e.role == "editor" and e.type == "step_started" for e in events)
    assert len([e for e in events if e.type == "proposal"]) == len(decisions)


def test_planner_schema_compiles_with_existing_grammar(project):
    from app.backend.ai.gbnf import schema_to_gbnf
    planner = Planner(ChangeSet(project), "Change value", lambda *_: "",
                      notify=lambda *_: None, check_cancelled=lambda: None, seconds_left=lambda: 60)
    for forced in (False, True):
        grammar = schema_to_gbnf(planner.schema(forced))
        assert 'root ::=' in grammar
        assert planner.schema(forced)["properties"]["files"]["type"] == "string"


def test_missing_path_is_repaired_before_tool_execution(project):
    replies = iter([
        json.dumps({"why": "inspect", "action": "read_file"}),
        json.dumps({"why": "correct path", "action": "read_file", "path": "app.py"}),
        json.dumps(dict(plan_action(), files="app.py")),
    ])
    notifications = []
    planner = Planner(ChangeSet(project), "Change value", lambda *_: next(replies),
                      notify=lambda *args: notifications.append(args),
                      check_cancelled=lambda: None, seconds_left=lambda: 60)
    plan = planner.run()
    assert plan.files == ("app.py",)
    assert planner.tool_calls == 1
    assert "value = 1" in str(notifications)
    assert (project.root / "app.py").read_text() == "value = 1\n"


@pytest.mark.parametrize("action", [
    {"action": "read_file", "arguments": {"path": "app.py"}},
    {"action": "read_file", "path": " "},
    {"action": "search_files"},
    {"action": "read_file", "path": "app.py", "start_line": False},
])
def test_malformed_planner_actions_are_rejected_early(project, action):
    planner = Planner(ChangeSet(project), "Change value", lambda *_: "",
                      notify=lambda *_: None, check_cancelled=lambda: None, seconds_left=lambda: 60)
    with pytest.raises(ValueError):
        planner.parse(json.dumps(action), False)


@pytest.mark.parametrize('key,limit', [('summary', 400), ('verification', 240), ('out_of_scope', 240)])
def test_verbose_plans_rejected_even_without_grammar(project, key, limit):
    planner = Planner(ChangeSet(project), 'Change value', lambda *_: '',
                      notify=lambda *_: None, check_cancelled=lambda: None, seconds_left=lambda: 60)
    action = plan_action()
    action[key] = 'x' * (limit + 1)
    with pytest.raises(ValueError, match='must be concise'):
        planner.parse(json.dumps(action), False)


def test_editor_receives_compact_contract_not_full_plan():
    from app.backend.agents.planner import EditPlan
    state = dict(params={'instruction': 'Insert exactly one line'},
                 plan=EditPlan('Add the requested assignment', ('app.py',),
                               'VERIFICATION_NOT_FOR_EDITOR', 'No unrelated changes'))
    text = tasks._editor_instruction(state)
    assert 'app.py' in text and 'Insert exactly one line' in text
    assert 'VERIFICATION_NOT_FOR_EDITOR' not in text
    assert 'EditPlan(' not in text


def test_model_asker_preserves_typed_output_limit():
    from app.backend.ai.base import AIOutputLimitError
    class Service:
        def generate_structured(self, *args, **kwargs):
            raise AIOutputLimitError('cut off')
    ask = tasks._model_asker(SimpleNamespace(), SimpleNamespace(service=Service()), 500)
    with pytest.raises(AIOutputLimitError):
        ask([], {})


def test_provider_discards_truncated_json_with_typed_error():
    from app.backend.ai.base import AIOutputLimitError
    from app.backend.ai.llama_provider import LlamaServerProvider
    raw = json.dumps({'choices': [{'finish_reason': 'length',
                                  'message': {'content': '{"action":"replace"'}}]})
    with pytest.raises(AIOutputLimitError, match='output token limit'):
        LlamaServerProvider._reply_text(raw)
