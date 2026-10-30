import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.backend.agents import service, tasks
from app.backend.agents.changes import read_manifest
from app.backend.agents.events import Event
from app.backend.agents.manager import get_run_manager
from app.backend.agents.supervisor import Limits, Step
from app.backend.agents.tasks import Task
from app.backend.database import Base
from app.backend.main import app
from app.backend.migrations_runner import run_migrations
from app.backend.models import AgentRun, RunEvent, User

PASSWORD = "TestPassword123!"


# ---------------------------------------------------------------- helpers

def new_client():
    """A logged-in client for a brand-new user (returns the client and its name)."""
    client = TestClient(app)
    username = f"runs_{uuid.uuid4().hex[:10]}"

    assert client.post(
        "/auth/register", json={"username": username, "password": PASSWORD}
    ).status_code == 201
    assert client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    ).status_code == 200

    client.username = username
    return client


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        value = predicate()

        if value:
            return value

        time.sleep(0.02)

    raise AssertionError("timed out waiting for the run")


def wait_until_finished(client, run_id):
    def finished():
        run = client.get(f"/runs/{run_id}").json()
        return run if not run["active"] else None

    return wait_for(finished)


def start(client, kind="self_check"):
    response = client.post("/runs/", json={"kind": kind})
    assert response.status_code == 201, response.text
    return response.json()


def user_id_of(session_factory, username):
    with session_factory() as db:
        return db.scalar(select(User.id).where(User.username == username))


@pytest.fixture
def slow_task(monkeypatch):
    """A task that stays running until released, so tests can act mid-run."""
    release = threading.Event()

    def wait(ctx):
        while not release.is_set():
            ctx.check_cancelled()
            time.sleep(0.01)
        return "released"

    task = Task(
        kind="slow",
        title="Slow task",
        description="Waits until released.",
        build=lambda params: [Step("wait", "Wait", wait)],
        limits=Limits(max_steps=3, max_seconds=60),
    )
    monkeypatch.setitem(tasks.TASKS, "slow", task)

    yield release

    release.set()  # never leave a worker thread behind


# --------------------------------------------------------- migration agrees

def test_migration_creates_tables_matching_the_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    run_migrations(url)

    engine = create_engine(url)

    try:
        inspector = inspect(engine)

        for name in ("agent_runs", "run_events"):
            built = {c["name"]: c["nullable"] for c in inspector.get_columns(name)}
            wanted = {c.name: c.nullable for c in Base.metadata.tables[name].columns}

            assert built == wanted, name

        assert "ix_agent_runs_user_id" in {i["name"] for i in inspector.get_indexes("agent_runs")}
        assert "ix_run_events_run_id" in {i["name"] for i in inspector.get_indexes("run_events")}
    finally:
        engine.dispose()


# ------------------------------------------------------------- the API

def test_everything_needs_login():
    anonymous = TestClient(app)

    assert anonymous.get("/runs/kinds").status_code == 401
    assert anonymous.get("/runs/").status_code == 401
    assert anonymous.post("/runs/", json={"kind": "self_check"}).status_code == 401
    assert anonymous.get("/runs/x").status_code == 401
    assert anonymous.get("/runs/x/events").status_code == 401
    assert anonymous.post("/runs/x/cancel").status_code == 401
    assert anonymous.delete("/runs/x").status_code == 401
    assert anonymous.get("/runs").status_code in (401, 307)  # the page itself


def test_kinds_lists_both_tasks_and_describes_their_forms():
    client = new_client()

    body = {k["kind"]: k for k in client.get("/runs/kinds").json()}

    assert list(body) == ["self_check", "ask_folder", "edit_files"]
    assert [(f["name"], f["kind"]) for f in body["edit_files"]["fields"]] == [
        ("folder", "text"),
        ("instruction", "textarea"),
        ("verification", "text"),
        ("python", "text"),
    ]
    assert body["self_check"]["title"] == "Self-check" and body["self_check"]["description"]
    assert body["self_check"]["fields"] == []
    assert [(f["name"], f["kind"]) for f in body["ask_folder"]["fields"]] == [
        ("folder", "text"),
        ("question", "textarea"),
    ]
    assert all(f["label"] and f["placeholder"] for f in body["ask_folder"]["fields"])


def test_a_self_check_runs_to_completion_with_a_full_log():
    client = new_client()

    created = start(client)
    assert created["kind"] == "self_check" and created["title"] == "Self-check"
    assert created["status"] in ("queued", "running")

    run = wait_until_finished(client, created["id"])

    assert run["status"] == "completed"
    assert run["summary"].endswith("steps finished") or "skipped" in run["summary"]
    assert run["finished_at"] and run["started_at"]
    assert run["created_at"].endswith("+00:00")

    events = client.get(f"/runs/{created['id']}/events").json()["events"]

    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))   # 1, 2, 3...
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_finished"
    assert [e["type"] for e in events].count("step_started") == 4
    assert all(e["title"] for e in events)
    assert [e["elapsed_ms"] for e in events] == sorted(e["elapsed_ms"] for e in events)


def test_events_after_returns_only_newer_ones():
    client = new_client()
    run = wait_until_finished(client, start(client)["id"])

    everything = client.get(f"/runs/{run['id']}/events").json()["events"]
    newer = client.get(f"/runs/{run['id']}/events", params={"after": 3}).json()

    assert [e["seq"] for e in newer["events"]] == [e["seq"] for e in everything if e["seq"] > 3]
    assert newer["run"]["status"] == "completed"
    assert client.get(f"/runs/{run['id']}/events", params={"after": 9999}).json()["events"] == []
    assert client.get(f"/runs/{run['id']}/events", params={"after": -1}).status_code == 422


def test_runs_are_listed_newest_first():
    client = new_client()
    first = wait_until_finished(client, start(client)["id"])
    second = wait_until_finished(client, start(client)["id"])

    listed = client.get("/runs/").json()

    assert [r["id"] for r in listed] == [second["id"], first["id"]]


def test_unknown_or_missing_kind_is_rejected():
    client = new_client()

    assert client.post("/runs/", json={"kind": "nope"}).status_code == 422
    assert client.post("/runs/", json={}).status_code == 422


def test_only_one_run_at_a_time(slow_task):
    client = new_client()
    first = start(client, "slow")

    refused = client.post("/runs/", json={"kind": "self_check"})

    assert refused.status_code == 409
    assert "already in progress" in refused.json()["detail"]

    slow_task.set()
    wait_until_finished(client, first["id"])

    assert client.post("/runs/", json={"kind": "self_check"}).status_code == 201


def test_a_different_user_is_not_blocked(slow_task):
    one, two = new_client(), new_client()
    start(one, "slow")

    assert two.post("/runs/", json={"kind": "self_check"}).status_code == 201


def test_cancel_stops_a_running_run(slow_task):
    client = new_client()
    run = start(client, "slow")

    wait_for(lambda: client.get(f"/runs/{run['id']}").json()["status"] == "running")
    response = client.post(f"/runs/{run['id']}/cancel")

    assert response.status_code == 200

    final = wait_until_finished(client, run["id"])
    events = client.get(f"/runs/{run['id']}/events").json()["events"]

    assert final["status"] == "cancelled"
    assert events[-1]["type"] == "run_finished" and "cancelled" in events[-1]["title"]
    assert not get_run_manager().is_live(run["id"])


def test_cancelling_a_finished_run_is_a_conflict():
    client = new_client()
    run = wait_until_finished(client, start(client)["id"])

    response = client.post(f"/runs/{run['id']}/cancel")

    assert response.status_code == 409
    assert "already finished" in response.json()["detail"]


def test_cancel_closes_a_run_that_nothing_is_running(testing_session_local):
    """E.g. left over after a crash: marked active, but no worker thread."""
    client = new_client()

    with testing_session_local() as db:
        run = service.create_run(db, user_id_of(testing_session_local, client.username), "self_check", "Orphan")
        run_id = run.id
        db.execute(AgentRun.__table__.update().where(AgentRun.id == run_id).values(status="running"))
        db.commit()

    response = client.post(f"/runs/{run_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert client.post("/runs/", json={"kind": "self_check"}).status_code == 201   # no longer blocked


def test_delete_removes_a_finished_run_and_its_log(testing_session_local):
    client = new_client()
    run = wait_until_finished(client, start(client)["id"])

    assert client.delete(f"/runs/{run['id']}").json() == {"deleted": True}
    assert client.get(f"/runs/{run['id']}").status_code == 404
    assert client.get("/runs/").json() == []

    with testing_session_local() as db:
        assert db.scalars(select(RunEvent).where(RunEvent.run_id == run["id"])).all() == []


def test_a_running_run_cannot_be_deleted(slow_task):
    client = new_client()
    run = start(client, "slow")

    response = client.delete(f"/runs/{run['id']}")

    assert response.status_code == 409
    assert "Cancel the run" in response.json()["detail"]
    assert client.get(f"/runs/{run['id']}").status_code == 200


def test_runs_are_private():
    owner, stranger = new_client(), new_client()
    run = wait_until_finished(owner, start(owner)["id"])

    assert stranger.get(f"/runs/{run['id']}").status_code == 404
    assert stranger.get(f"/runs/{run['id']}/events").status_code == 404
    assert stranger.post(f"/runs/{run['id']}/cancel").status_code == 404
    assert stranger.delete(f"/runs/{run['id']}").status_code == 404
    assert stranger.get("/runs/").json() == []
    assert owner.get(f"/runs/{uuid.uuid4()}").status_code == 404

    assert owner.get(f"/runs/{run['id']}").status_code == 200


# ----------------------------------------------------- the database layer

def test_event_sink_numbers_events_and_clips_long_text(test_engine):
    with Session(test_engine) as db:
        owner = User(username=f"sink_{uuid.uuid4().hex[:8]}", password_hash="x")
        db.add(owner)
        db.commit()
        run = service.create_run(db, owner.id, "self_check", "Sink test")
        run_id = run.id

    sink = service.DbEventSink(test_engine, run_id)
    sink.emit(Event("run_started", "Start", role="supervisor", elapsed_ms=0))
    sink.emit(Event("info", "T" * 500, role="reader", detail="D" * 50_000, duration_ms=7, elapsed_ms=40))

    with Session(test_engine) as db:
        events = service.events_after(db, run_id)

    assert [e.seq for e in events] == [1, 2]
    assert events[1].role == "reader" and events[1].duration_ms == 7 and events[1].elapsed_ms == 40
    assert len(events[1].title) == service.TITLE_LIMIT and events[1].title.endswith("\u2026")
    assert len(events[1].detail) == service.DETAIL_LIMIT


def test_finish_run_never_reopens_or_overwrites_a_finished_run(test_engine):
    with Session(test_engine) as db:
        owner = User(username=f"fin_{uuid.uuid4().hex[:8]}", password_hash="x")
        db.add(owner)
        db.commit()
        run_id = service.create_run(db, owner.id, "self_check", "Finish test").id

    service.finish_run(test_engine, run_id, "completed", "first", None)
    service.finish_run(test_engine, run_id, "failed", None, "second try")

    with Session(test_engine) as db:
        run = db.get(AgentRun, run_id)

    assert run.status == "completed" and run.summary == "first" and run.error is None


def test_interrupted_runs_are_failed_at_startup(test_engine):
    with Session(test_engine) as db:
        owner = User(username=f"crash_{uuid.uuid4().hex[:8]}", password_hash="x")
        db.add(owner)
        db.commit()
        busy = service.create_run(db, owner.id, "self_check", "Was running")
        busy_id = busy.id
        done_id = AgentRun(user_id=owner.id, kind="self_check", title="Done", status="completed")
        db.add(done_id)
        db.commit()
        done_id = done_id.id

    sink = service.DbEventSink(test_engine, busy_id)
    sink.emit(Event("run_started", "Start", elapsed_ms=0))
    sink.emit(Event("step_started", "Step", elapsed_ms=1500))

    count = service.fail_interrupted_runs(test_engine)

    with Session(test_engine) as db:
        busy_after = db.get(AgentRun, busy_id)
        done_after = db.get(AgentRun, done_id)
        events = service.events_after(db, busy_id)

    assert count >= 1
    assert busy_after.status == "failed" and busy_after.error == service.INTERRUPTED_MESSAGE
    assert busy_after.finished_at is not None
    assert done_after.status == "completed" and done_after.error is None   # untouched
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[-1].type == "run_finished" and events[-1].elapsed_ms == 1500
    assert service.fail_interrupted_runs(test_engine) == 0                  # idempotent


# ------------------------------------------- asking about a folder (params)

class ScriptedService:
    def __init__(self, *replies):
        self.replies = list(replies)

    def generate_structured(self, messages, schema, *, max_tokens=None):
        if "verification" in schema.get("properties", {}):
            return json.dumps({"why": "plan", "action": "finish", "summary": "Change main to return 2",
                               "files": list(dict.fromkeys(["src/app.py"] + [r["path"] for r in self.replies if r.get("action") in ("replace", "create")])), "verification": "Proposed pytest; not run",
                               "out_of_scope": "No unrelated changes"})
        return json.dumps(self.replies.pop(0))


def use_scripted_model(monkeypatch, *replies):
    status = {
        "provider": "llama", "state": "ready", "detail": None, "model": "qwen.gguf",
        "models": [], "can_switch": True, "setup": None,
    }
    runtime = SimpleNamespace(
        status=lambda: status,
        service=ScriptedService(*replies),
        settings=SimpleNamespace(ctx_size=8192),
    )
    monkeypatch.setattr(tasks, "get_runtime", lambda: runtime)


def make_folder(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("print('hello world')\n", encoding="utf-8")
    return root


def test_a_question_about_a_folder_is_answered_and_everything_is_stored(tmp_path, monkeypatch):
    root = make_folder(tmp_path)
    use_scripted_model(
        monkeypatch,
        {"why": "look", "action": "list_files", "path": "."},
        {"why": "open it", "action": "read_file", "path": "app.py"},
        {"why": "done", "action": "answer", "answer": "It prints **hello world** (`app.py:1`)."},
    )
    client = new_client()

    created = client.post(
        "/runs/",
        json={"kind": "ask_folder", "params": {"folder": f' "{root}" ', "question": "What does it print?"}},
    )

    assert created.status_code == 201
    run = created.json()
    assert run["title"] == "Ask: What does it print?"
    assert run["params"] == {"folder": str(root.resolve()), "question": "What does it print?"}   # cleaned

    final = wait_until_finished(client, run["id"])

    assert final["status"] == "completed"
    assert final["summary"] == (
        "It prints **hello world** (`app.py:1`).\n\n**Files read:** `app.py` (all 1 line)"
    )
    assert final["params"] == run["params"]

    events = client.get(f"/runs/{run['id']}/events").json()["events"]
    tool_calls = [e for e in events if e["type"] == "tool_call"]

    assert [e["title"] for e in tool_calls] == ["list_files .", "read_file app.py"]
    assert all(e["role"] == "reader" and e["duration_ms"] is not None for e in tool_calls)
    assert "Why: look" in tool_calls[0]["detail"] and "hello world" in tool_calls[1]["detail"]

    assert client.get("/runs/").json()[0]["params"] == run["params"]


def test_invalid_parameters_are_rejected_with_the_reason(tmp_path):
    client = new_client()
    base = {"kind": "ask_folder"}

    cases = [
        ({}, "Enter the folder"),
        ({"folder": str(tmp_path)}, "Enter a question"),
        ({"folder": str(tmp_path / "missing"), "question": "q"}, "does not exist"),
        ({"folder": "relative/path", "question": "q"}, "full path"),
        ({"folder": str(tmp_path), "question": "q" * 1001}, "too long"),
    ]

    for params, message in cases:
        response = client.post("/runs/", json={**base, "params": params})

        assert response.status_code == 422, params
        assert message in response.json()["detail"]

    assert client.get("/runs/").json() == []                    # nothing was created
    assert client.post("/runs/", json={**base, "params": "not a dict"}).status_code == 422


def test_a_rejected_form_does_not_block_the_next_run(tmp_path, monkeypatch):
    client = new_client()

    assert client.post("/runs/", json={"kind": "ask_folder", "params": {}}).status_code == 422
    assert client.post("/runs/", json={"kind": "self_check"}).status_code == 201


def test_asking_about_a_folder_in_echo_mode_fails_with_a_clear_reason(tmp_path):
    root = make_folder(tmp_path)
    client = new_client()

    run = start_with(client, "ask_folder", {"folder": str(root), "question": "What is this?"})
    final = wait_until_finished(client, run["id"])

    assert final["status"] == "failed"
    assert "echo mode" in final["error"]


def test_self_check_ignores_stray_parameters():
    client = new_client()

    response = client.post("/runs/", json={"kind": "self_check", "params": {"junk": 1}})

    assert response.status_code == 201
    assert response.json()["params"] == {}


def test_the_off_limits_data_folder_cannot_be_asked_about(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(tasks, "DATA_DIR", data)
    client = new_client()

    response = client.post("/runs/", json={"kind": "ask_folder", "params": {"folder": str(data), "question": "q"}})

    assert response.status_code == 422 and "off limits" in response.json()["detail"]


def test_the_allowed_roots_setting_is_enforced_by_the_api(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    elsewhere = make_folder(tmp_path)
    monkeypatch.setenv("ORION_AGENT_ROOTS", str(allowed))
    client = new_client()

    response = client.post("/runs/", json={"kind": "ask_folder", "params": {"folder": str(elsewhere), "question": "q"}})

    assert response.status_code == 422 and "ORION_AGENT_ROOTS" in response.json()["detail"]


def start_with(client, kind, params):
    response = client.post("/runs/", json={"kind": kind, "params": params})
    assert response.status_code == 201, response.text
    return response.json()


# ================================================== editing files: approve, undo

APP_TEXT = "import os\n\ndef main():\n    print('hello world')\n    return 1\n"


def edit_folder(tmp_path):
    root = tmp_path / "editme"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(APP_TEXT, newline="\n")

    return root


def one_edit(monkeypatch):
    use_scripted_model(
        monkeypatch,
        {"why": "look", "action": "read_file", "path": "src/app.py"},
        {"why": "change", "action": "replace", "path": "src/app.py", "find": "    return 1", "replace": "    return 2"},
        {"why": "done", "action": "finish", "summary": "main now returns 2."},
    )


def start_edit(client, root, instruction="Make main return 2"):
    run = start_with(client, "edit_files", {"folder": str(root), "instruction": instruction})
    # Legacy API cases below start at the final diff gate. New plan-gate
    # rejection/approval behavior has dedicated integration coverage.
    wait_for_status(client, run["id"], "awaiting_approval")
    assert client.post(f"/runs/{run['id']}/decision", json={"decision": "apply"}).status_code == 200
    wait_for(lambda: any(e["type"] == "step_finished" and "Plan approved." in (e.get("detail") or "")
                        for e in client.get(f"/runs/{run['id']}/events").json()["events"]))
    return run


def wait_for_status(client, run_id, status):
    return wait_for(lambda: (r := client.get(f"/runs/{run_id}").json())["status"] == status and r)


def app_text(root):
    return (root / "src" / "app.py").read_text()


def test_an_edit_waits_for_approval_then_writes_and_can_be_undone(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    one_edit(monkeypatch)
    client = new_client()

    run = start_edit(client, root)
    assert run["title"] == "Edit: Make main return 2"

    waiting = wait_for_status(client, run["id"], "awaiting_approval")
    events = client.get(f"/runs/{run['id']}/events").json()["events"]
    proposal = [e for e in events if e["type"] == "proposal" and e["title"].startswith("Proposed changes:")][0]

    assert waiting["active"] is True and "changes" not in waiting
    assert proposal["title"] == "Proposed changes: 1 file (+1 \u22121)"
    assert "--- a/src/app.py" in proposal["detail"] and "+    return 2" in proposal["detail"]
    assert app_text(root) == APP_TEXT                                          # nothing written while waiting

    decided = client.post(f"/runs/{run['id']}/decision", json={"decision": "apply"})
    assert decided.status_code == 200

    final = wait_until_finished(client, run["id"])

    assert final["status"] == "completed"
    assert app_text(root) == APP_TEXT.replace("return 1", "return 2")
    assert final["summary"].startswith("**Applied 1 file change** (+1 \u22121): `src/app.py`.")
    assert final["changes"]["applied"] is True and final["changes"]["undone"] is False
    assert final["changes"]["files"] == ["src/app.py"]
    assert client.get("/runs/").json()[0]["changes"]["files"] == ["src/app.py"]
    assert client.get(f"/runs/{run['id']}/events").json()["run"]["changes"]["applied"] is True

    undone = client.post(f"/runs/{run['id']}/undo")

    assert undone.status_code == 200
    assert undone.json()["undo"] == {"restored": ["src/app.py"], "deleted": [], "skipped": []}
    assert undone.json()["run"]["changes"]["undone"] is True
    assert app_text(root) == APP_TEXT

    events = client.get(f"/runs/{run['id']}/events").json()["events"]
    assert events[-1]["title"] == "Undo: restored 1" and "Restored: src/app.py" in events[-1]["detail"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert client.post(f"/runs/{run['id']}/undo").status_code == 409          # only once


def test_rejecting_changes_nothing_and_leaves_nothing_to_undo(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    one_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")

    assert client.post(f"/runs/{run['id']}/decision", json={"decision": "reject"}).status_code == 200

    final = wait_until_finished(client, run["id"])

    assert final["status"] == "completed" and "changes" not in final
    assert final["summary"].startswith("You rejected the proposed changes; nothing was written.")
    assert app_text(root) == APP_TEXT
    assert client.post(f"/runs/{run['id']}/undo").status_code == 409


def test_cancelling_while_waiting_changes_nothing(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    one_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")

    assert client.post(f"/runs/{run['id']}/cancel").status_code == 200

    assert wait_until_finished(client, run["id"])["status"] == "cancelled"
    assert app_text(root) == APP_TEXT


def test_only_a_waiting_run_accepts_a_decision(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    one_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")

    assert client.post(f"/runs/{run['id']}/decision", json={"decision": "maybe"}).status_code == 422
    assert client.post(f"/runs/{run['id']}/decision", json={}).status_code == 422
    assert client.post(f"/runs/{run['id']}/decision", json={"decision": "apply"}).status_code == 200

    wait_until_finished(client, run["id"])
    late = client.post(f"/runs/{run['id']}/decision", json={"decision": "apply"})

    assert late.status_code == 409 and "not waiting" in late.json()["detail"]


def test_a_run_that_is_not_an_edit_cannot_be_decided():
    client = new_client()
    run = wait_until_finished(client, start(client)["id"])

    assert client.post(f"/runs/{run['id']}/decision", json={"decision": "apply"}).status_code == 409


def test_a_waiting_run_still_blocks_new_runs_and_cannot_be_undone_or_deleted(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    one_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")

    assert client.post("/runs/", json={"kind": "self_check"}).status_code == 409
    assert client.post(f"/runs/{run['id']}/undo").status_code == 409
    assert client.delete(f"/runs/{run['id']}").status_code == 409

    client.post(f"/runs/{run['id']}/cancel")
    wait_until_finished(client, run["id"])


def test_decisions_and_undo_are_private_and_need_login(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    one_edit(monkeypatch)
    owner, stranger = new_client(), new_client()
    run = start_edit(owner, root)
    wait_for_status(owner, run["id"], "awaiting_approval")
    path = f"/runs/{run['id']}"

    assert stranger.post(path + "/decision", json={"decision": "apply"}).status_code == 404
    assert stranger.post(path + "/undo").status_code == 404
    assert TestClient(app).post(path + "/decision", json={"decision": "apply"}).status_code == 401
    assert TestClient(app).post(path + "/undo").status_code == 401
    assert app_text(root) == APP_TEXT

    owner.post(path + "/cancel")
    wait_until_finished(owner, run["id"])


def test_deleting_a_run_also_deletes_its_backup(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    one_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")
    client.post(f"/runs/{run['id']}/decision", json={"decision": "apply"})
    wait_until_finished(client, run["id"])
    backup = tasks.backup_dir_of(run["id"])

    assert read_manifest(backup) is not None

    assert client.delete(f"/runs/{run['id']}").json() == {"deleted": True}
    assert not backup.exists()


def test_a_file_changed_while_waiting_is_not_overwritten(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    one_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")
    (root / "src" / "app.py").write_text("# I kept working\n")

    client.post(f"/runs/{run['id']}/decision", json={"decision": "apply"})
    final = wait_until_finished(client, run["id"])

    assert final["status"] == "failed" and "changed since the snapshot" in final["error"]
    assert app_text(root) == "# I kept working\n" and "changes" not in final


def test_invalid_edit_parameters_are_rejected(tmp_path):
    client = new_client()

    for params, message in (
        ({}, "Enter the folder to change"),
        ({"folder": str(tmp_path)}, "Describe the change"),
        ({"folder": str(tmp_path / "missing"), "instruction": "x"}, "does not exist"),
        ({"folder": tmp_path.anchor, "instruction": "x"}, "specific project folder"),
    ):
        response = client.post("/runs/", json={"kind": "edit_files", "params": params})

        assert response.status_code == 422 and message in response.json()["detail"], params


# --------------------------------------------------- the database side of waiting

def test_a_run_left_waiting_by_a_previous_process_is_failed_at_startup(test_engine):
    with Session(test_engine) as db:
        owner = User(username=f"wait_{uuid.uuid4().hex[:8]}", password_hash="x")
        db.add(owner)
        db.commit()
        run_id = service.create_run(db, owner.id, "edit_files", "Waiting").id

    service.set_status(test_engine, run_id, "awaiting_approval")

    with Session(test_engine) as db:
        assert db.get(AgentRun, run_id).status == "awaiting_approval"

    assert service.fail_interrupted_runs(test_engine) >= 1

    with Session(test_engine) as db:
        run = db.get(AgentRun, run_id)

    assert run.status == "failed" and run.error == service.INTERRUPTED_MESSAGE


def test_set_status_never_reopens_a_finished_run(test_engine):
    with Session(test_engine) as db:
        owner = User(username=f"reopen_{uuid.uuid4().hex[:8]}", password_hash="x")
        db.add(owner)
        db.commit()
        run_id = service.create_run(db, owner.id, "edit_files", "Done").id

    service.finish_run(test_engine, run_id, "completed", "ok", None)
    service.set_status(test_engine, run_id, "awaiting_approval")

    with Session(test_engine) as db:
        assert db.get(AgentRun, run_id).status == "completed"


def test_events_can_be_appended_after_a_run_has_finished(test_engine):
    with Session(test_engine) as db:
        owner = User(username=f"append_{uuid.uuid4().hex[:8]}", password_hash="x")
        db.add(owner)
        db.commit()
        run_id = service.create_run(db, owner.id, "edit_files", "Append").id

    sink = service.DbEventSink(test_engine, run_id)
    sink.emit(Event("run_started", "Start", elapsed_ms=0))
    sink.emit(Event("run_finished", "Finished", elapsed_ms=9000))

    service.append_event(test_engine, run_id, Event("info", "Undo: restored 1", detail="x", elapsed_ms=0))

    with Session(test_engine) as db:
        events = service.events_after(db, run_id)

    assert [e.seq for e in events] == [1, 2, 3]
    assert events[-1].title == "Undo: restored 1" and events[-1].elapsed_ms == 9000      # never goes backwards


# ============================================ security checks and risk acknowledgement

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def risky_edit(monkeypatch):
    use_scripted_model(
        monkeypatch,
        {"why": "change", "action": "replace", "path": "src/app.py", "find": "    return 1",
         "replace": f'    return 1\n    key = "{AWS_KEY}"'},
        {"why": "done", "action": "finish", "summary": "added a line"},
    )


def test_the_security_check_is_logged_before_the_diff_and_never_holds_a_secret(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    risky_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")

    events = client.get(f"/runs/{run['id']}/events").json()["events"]
    kinds = [e["type"] for e in events if e["type"] in ("check", "proposal")]
    event = [e for e in events if e["type"] == "check"][0]
    finding = json.loads(event["detail"])["findings"][0]

    assert kinds == ["proposal", "check", "proposal"] and event["role"] == "security"
    assert event["title"] == "Security checks: 1 high-risk finding"
    assert finding["code"] == "aws_key" and finding["severity"] == "high" and "AKIA****" in finding["excerpt"]
    assert AWS_KEY not in json.dumps(events)                  # the secret is nowhere in the stored log

    client.post(f"/runs/{run['id']}/cancel")
    wait_until_finished(client, run["id"])


def test_a_high_risk_change_is_refused_until_it_is_acknowledged(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    risky_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")
    path = f"/runs/{run['id']}/decision"

    refused = client.post(path, json={"decision": "apply"})
    explicitly_not = client.post(path, json={"decision": "apply", "acknowledge_risks": False})

    assert refused.status_code == 409 and "high-risk" in refused.json()["detail"]
    assert explicitly_not.status_code == 409
    assert client.get(f"/runs/{run['id']}").json()["status"] == "awaiting_approval"      # still waiting
    assert app_text(root) == APP_TEXT

    accepted = client.post(path, json={"decision": "apply", "acknowledge_risks": True})
    assert accepted.status_code == 200

    final = wait_until_finished(client, run["id"])

    assert final["status"] == "completed" and AWS_KEY in app_text(root)
    assert "Security checks: 1 high-risk finding." in final["summary"]
    assert "You confirmed the high-risk findings before applying." in final["summary"]


def test_rejecting_a_high_risk_change_needs_no_acknowledgement(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    risky_edit(monkeypatch)
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")

    assert client.post(f"/runs/{run['id']}/decision", json={"decision": "reject"}).status_code == 200

    assert wait_until_finished(client, run["id"])["status"] == "completed"
    assert app_text(root) == APP_TEXT


def test_changes_without_high_risk_findings_never_need_an_acknowledgement(tmp_path, monkeypatch):
    root = edit_folder(tmp_path)
    use_scripted_model(
        monkeypatch,
        {"why": "new script", "action": "create", "path": "deploy.ps1", "content": "Write-Host hello\n"},
        {"why": "done", "action": "finish", "summary": "added a script"},
    )
    client = new_client()
    run = start_edit(client, root)
    wait_for_status(client, run["id"], "awaiting_approval")
    events = client.get(f"/runs/{run['id']}/events").json()["events"]

    assert [e["title"] for e in events if e["type"] == "check"] == ["Security checks: 1 warning"]
    assert client.post(f"/runs/{run['id']}/decision", json={"decision": "apply"}).status_code == 200
    assert wait_until_finished(client, run["id"])["status"] == "completed" and (root / "deploy.ps1").exists()

