import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.backend.ai import routes as ai_routes
from app.backend.ai.llama_server import (
    LlamaServerManager,
    platform_tag,
    server_binary_name,
)
from app.backend.ai.model_catalog import (
    choose_initial_model,
    list_models,
    load_selection,
    save_selection,
)
from app.backend.ai.runtime import (
    AISettings,
    ModelSwitchError,
    build_runtime,
)
from app.backend.main import app

FAKE_SERVER = Path(__file__).with_name("fake_llama_server.py")


def make_install(base_dir: Path, models=("a.gguf", "b.gguf"), binary=True):
    if binary:
        folder = base_dir / "runtime" / "llama" / platform_tag()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / server_binary_name()).write_bytes(b"x")

    (base_dir / "models").mkdir(exist_ok=True)

    for name in models:
        (base_dir / "models" / name).write_bytes(b"x" * 10)


@pytest.fixture
def no_launch(monkeypatch):
    """Never start a real llama-server process in these tests."""
    started = []
    monkeypatch.setattr(LlamaServerManager, "start", lambda self: started.append(self.model))
    monkeypatch.setattr(LlamaServerManager, "stop", lambda self: None)
    return started


# ---------------------------------------------------------------- catalog

def test_list_models_only_returns_selectable_chat_models(tmp_path):
    make_install(
        tmp_path,
        models=(
            "Zeta.gguf",
            "alpha.GGUF",
            "mmproj-F16.gguf",            # vision projector, not a model
            "big-00001-of-00003.gguf",    # first shard: selectable
            "big-00002-of-00003.gguf",    # other shards: hidden
            "big-00003-of-00003.gguf",
            "notes.txt",
        ),
        binary=False,
    )
    (tmp_path / "models" / "sub").mkdir()
    (tmp_path / "models" / "sub" / "hidden.gguf").write_bytes(b"x")

    models = list_models(tmp_path)

    assert [m.id for m in models] == [
        "alpha.GGUF",
        "big-00001-of-00003.gguf",
        "Zeta.gguf",
    ]
    assert models[0].name == "alpha"
    assert models[0].size == 10


def test_list_models_without_folder_is_empty(tmp_path):
    assert list_models(tmp_path) == []


def test_selection_round_trip_and_fallback(tmp_path):
    make_install(tmp_path, binary=False)
    data = tmp_path / "data"
    data.mkdir()

    assert load_selection(data) is None
    assert choose_initial_model(tmp_path, data).name == "a.gguf"

    save_selection(data, "b.gguf")
    assert load_selection(data) == "b.gguf"
    assert choose_initial_model(tmp_path, data).name == "b.gguf"

    # The remembered file was deleted: fall back instead of failing.
    (tmp_path / "models" / "b.gguf").unlink()
    assert choose_initial_model(tmp_path, data).name == "a.gguf"


# ---------------------------------------------------------------- runtime

def test_build_runtime_uses_remembered_model_and_lists_all(tmp_path, no_launch):
    make_install(tmp_path)
    save_selection(tmp_path, "b.gguf")

    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)
    status = runtime.status()

    assert status["model"] == "b.gguf"
    assert [m["id"] for m in status["models"]] == ["a.gguf", "b.gguf"]
    assert status["can_switch"] is True


def test_explicit_model_path_pins_the_model(tmp_path, no_launch):
    make_install(tmp_path)
    pinned = tmp_path / "models" / "a.gguf"

    runtime = build_runtime(
        AISettings(provider="auto", model_path=str(pinned)), tmp_path, tmp_path
    )
    status = runtime.status()

    assert status["model"] == "a.gguf"
    assert status["can_switch"] is False
    assert [m["id"] for m in status["models"]] == ["a.gguf"]

    with pytest.raises(ModelSwitchError) as error:
        runtime.select_model("b.gguf")

    assert error.value.reason == "locked"


def test_echo_mode_cannot_switch(tmp_path):
    make_install(tmp_path)

    runtime = build_runtime(AISettings(provider="echo"), tmp_path, tmp_path)

    assert runtime.status()["can_switch"] is False

    with pytest.raises(ModelSwitchError) as error:
        runtime.select_model("a.gguf")

    assert error.value.reason == "unavailable"


def test_select_unknown_model_is_rejected(tmp_path, no_launch):
    make_install(tmp_path)
    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)

    for bad in ("nope.gguf", "../a.gguf", str(tmp_path / "models" / "a.gguf")):
        with pytest.raises(ModelSwitchError) as error:
            runtime.select_model(bad)

        assert error.value.reason == "not_found"


def test_select_switches_model_and_remembers_it(tmp_path, no_launch):
    make_install(tmp_path)
    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)
    switched = []
    runtime.manager.switch_model = lambda path: switched.append(path.name)

    status = runtime.select_model("b.gguf")

    assert switched == ["b.gguf"]
    assert status["model"] == "b.gguf"
    assert load_selection(tmp_path) == "b.gguf"


def test_selecting_the_running_model_is_a_no_op(tmp_path, no_launch, monkeypatch):
    make_install(tmp_path)
    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)
    monkeypatch.setattr(LlamaServerManager, "state", property(lambda self: "ready"))
    runtime.manager.switch_model = lambda path: pytest.fail("should not restart")

    assert runtime.select_model("a.gguf")["model"] == "a.gguf"


def test_select_refused_while_a_reply_is_streaming(tmp_path, no_launch):
    make_install(tmp_path)
    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)
    runtime.manager.switch_model = lambda path: pytest.fail("must not interrupt a reply")

    runtime.provider._generation_lock.acquire()

    try:
        with pytest.raises(ModelSwitchError) as error:
            runtime.select_model("b.gguf")
    finally:
        runtime.provider._generation_lock.release()

    assert error.value.reason == "busy"
    assert load_selection(tmp_path) is None


def test_adding_the_first_model_later_brings_the_ai_online(tmp_path, no_launch):
    make_install(tmp_path, models=())  # engine present, no models yet

    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)

    assert runtime.kind == "echo"
    assert runtime.status()["models"] == []

    (tmp_path / "models" / "new.gguf").write_bytes(b"x")

    assert [m["id"] for m in runtime.status()["models"]] == ["new.gguf"]

    status = runtime.select_model("new.gguf")

    assert runtime.kind == "llama"
    assert status["provider"] == "llama"
    assert status["model"] == "new.gguf"
    assert no_launch == [tmp_path / "models" / "new.gguf"]


def test_real_manager_restarts_on_the_new_model(tmp_path):
    """End to end against the fake llama-server (no mocks)."""
    make_install(tmp_path)
    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)

    def fake_builder(port: int) -> list[str]:
        return [sys.executable, str(FAKE_SERVER), "--port", str(port)]

    runtime.manager._command_builder = fake_builder
    runtime.manager.startup_timeout = 15

    try:
        runtime.start()
        runtime.manager.wait_ready()

        status = runtime.select_model("b.gguf")

        assert status["model"] == "b.gguf"
        assert runtime.manager.model.name == "b.gguf"

        runtime.manager.wait_ready()  # the replacement process came up

        assert runtime.status()["state"] == "ready"
        assert runtime.manager.port is not None
    finally:
        runtime.stop()


# ---------------------------------------------------------------- HTTP API

client = TestClient(app)


def login():
    username = f"models_api_{uuid.uuid4().hex[:10]}"
    password = "TestPassword123!"

    assert client.post(
        "/auth/register", json={"username": username, "password": password}
    ).status_code == 201
    assert client.post(
        "/auth/login", json={"username": username, "password": password}
    ).status_code == 200


def test_status_lists_models_and_requires_login():
    anonymous = TestClient(app)

    assert anonymous.get("/ai/status").status_code == 401
    assert anonymous.post("/ai/model", json={"model": "a.gguf"}).status_code == 401

    login()
    body = client.get("/ai/status").json()

    assert "models" in body and "can_switch" in body


def test_select_model_in_echo_mode_is_a_conflict():
    login()

    response = client.post("/ai/model", json={"model": "a.gguf"})

    assert response.status_code == 409
    assert "cannot be switched" in response.json()["detail"]


def test_select_model_api_maps_errors_and_success(tmp_path, no_launch, monkeypatch):
    make_install(tmp_path)
    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)
    runtime.manager.switch_model = lambda path: None
    monkeypatch.setattr(ai_routes, "get_runtime", lambda: runtime)
    login()

    missing = client.post("/ai/model", json={"model": "nope.gguf"})
    assert missing.status_code == 404

    ok = client.post("/ai/model", json={"model": "b.gguf"})
    assert ok.status_code == 200
    assert ok.json()["model"] == "b.gguf"

    runtime.provider._generation_lock.acquire()
    try:
        busy = client.post("/ai/model", json={"model": "a.gguf"})
    finally:
        runtime.provider._generation_lock.release()

    assert busy.status_code == 409

    assert client.post("/ai/model", json={}).status_code == 422
