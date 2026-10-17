import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.backend.ai.llama_server import (
    LlamaServerManager,
    platform_tag,
    server_binary_name,
)
from app.backend.ai.runtime import AISettings, build_runtime
from app.backend.main import app


def add_engine(base_dir: Path) -> Path:
    folder = base_dir / "runtime" / "llama" / platform_tag()
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / server_binary_name()
    binary.write_bytes(b"x")
    return binary


def add_model(base_dir: Path, name: str = "model.gguf") -> None:
    (base_dir / "models").mkdir(exist_ok=True)
    (base_dir / "models" / name).write_bytes(b"x" * 10)


@pytest.fixture
def no_launch(monkeypatch):
    """Never start a real llama-server process in these tests."""
    started = []
    monkeypatch.setattr(LlamaServerManager, "start", lambda self: started.append(self.model))
    monkeypatch.setattr(LlamaServerManager, "stop", lambda self: None)
    return started


def runtime_for(base_dir: Path, **settings):
    return build_runtime(AISettings(provider="auto", **settings), base_dir, base_dir)


# ------------------------------------------------------------- setup info

def test_nothing_installed_reports_both_missing_with_exact_folders(tmp_path):
    setup = runtime_for(tmp_path).status()["setup"]

    assert setup["engine_missing"] is True
    assert setup["model_missing"] is True
    assert setup["models_dir"] == str(tmp_path / "models")
    assert setup["engine_dir"] == str(tmp_path / "runtime" / "llama" / platform_tag())


def test_only_the_missing_part_is_reported(tmp_path):
    add_engine(tmp_path)
    assert runtime_for(tmp_path).status()["setup"]["engine_missing"] is False
    assert runtime_for(tmp_path).status()["setup"]["model_missing"] is True

    other = tmp_path / "other"
    other.mkdir()
    add_model(other)
    setup = runtime_for(other).status()["setup"]

    assert setup["engine_missing"] is True and setup["model_missing"] is False


def test_no_setup_info_when_everything_is_in_place(tmp_path, no_launch):
    add_engine(tmp_path)
    add_model(tmp_path)

    assert runtime_for(tmp_path).status()["setup"] is None


def test_deliberate_echo_mode_is_not_a_setup_problem(tmp_path):
    runtime = build_runtime(AISettings(provider="echo"), tmp_path, tmp_path)

    assert runtime.status()["setup"] is None


def test_strict_llama_mode_also_shows_the_guide(tmp_path):
    runtime = build_runtime(AISettings(provider="llama"), tmp_path, tmp_path)
    status = runtime.status()

    assert status["provider"] == "unavailable"
    assert status["setup"]["engine_missing"] and status["setup"]["model_missing"]


# ---------------------------------------------------------------- recheck

def test_recheck_brings_the_ai_online_after_files_are_added(tmp_path, no_launch):
    runtime = runtime_for(tmp_path)
    assert runtime.kind == "echo"

    add_engine(tmp_path)
    add_model(tmp_path, "chat.gguf")

    status = runtime.recheck()

    assert runtime.kind == "llama"
    assert status["provider"] == "llama"
    assert status["model"] == "chat.gguf"
    assert status["setup"] is None
    assert no_launch == [tmp_path / "models" / "chat.gguf"]


def test_recheck_works_from_strict_llama_mode_too(tmp_path, no_launch):
    runtime = build_runtime(AISettings(provider="llama"), tmp_path, tmp_path)
    add_engine(tmp_path)
    add_model(tmp_path)

    assert runtime.recheck()["provider"] == "llama"


def test_recheck_with_a_partial_install_updates_the_guide(tmp_path, no_launch):
    runtime = runtime_for(tmp_path)

    add_engine(tmp_path)
    status = runtime.recheck()

    assert runtime.kind == "echo"                       # still not usable
    assert status["setup"]["engine_missing"] is False   # but progress is seen
    assert status["setup"]["model_missing"] is True
    assert "no llama-server" not in status["detail"]
    assert "no .gguf model" in status["detail"]
    assert no_launch == []


def test_recheck_with_nothing_new_changes_nothing(tmp_path, no_launch):
    runtime = runtime_for(tmp_path)

    before = runtime.status()

    assert runtime.recheck() == before
    assert no_launch == []


def test_recheck_never_restarts_a_running_ai(tmp_path, no_launch):
    add_engine(tmp_path)
    add_model(tmp_path)
    runtime = runtime_for(tmp_path)
    add_model(tmp_path, "another.gguf")

    status = runtime.recheck()

    assert status["model"] == "model.gguf"
    assert no_launch == []


def test_recheck_ignored_in_echo_mode(tmp_path, no_launch):
    runtime = build_runtime(AISettings(provider="echo"), tmp_path, tmp_path)
    add_engine(tmp_path)
    add_model(tmp_path)

    assert runtime.recheck()["provider"] == "echo"
    assert no_launch == []


# --------------------------------------------------------------- HTTP API

def test_recheck_endpoint_needs_login_then_reports_status():
    anonymous = TestClient(app)

    assert anonymous.post("/ai/recheck").status_code == 401

    client = TestClient(app)
    username = f"first_run_{uuid.uuid4().hex[:10]}"

    client.post("/auth/register", json={"username": username, "password": "TestPassword123!"})
    assert client.post(
        "/auth/login", json={"username": username, "password": "TestPassword123!"}
    ).status_code == 200

    body = client.post("/ai/recheck").json()

    assert "state" in body and "setup" in body
