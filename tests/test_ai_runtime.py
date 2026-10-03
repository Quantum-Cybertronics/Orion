import pytest

from app.backend.ai.base import AIProviderError
from app.backend.ai.llama_server import platform_tag, server_binary_name
from app.backend.ai.providers import EchoAIProvider, UnavailableAIProvider
from app.backend.ai.runtime import AISettings, build_runtime, load_settings
from app.backend.ai.service import AIService


def make_install(base_dir, *, binary=True, model=True):
    if binary:
        folder = base_dir / "runtime" / "llama" / platform_tag()
        folder.mkdir(parents=True)
        (folder / server_binary_name()).write_bytes(b"x")

    if model:
        (base_dir / "models").mkdir()
        (base_dir / "models" / "tiny.gguf").write_bytes(b"x")


def test_settings_defaults_and_overrides():
    assert load_settings({}).provider == "auto"

    settings = load_settings(
        {
            "ORION_AI_PROVIDER": " LLAMA ",
            "ORION_LLAMA_CTX": "8192",
            "ORION_LLAMA_THREADS": "6",
            "ORION_TEMPERATURE": "0.2",
        }
    )

    assert settings.provider == "llama"
    assert settings.ctx_size == 8192
    assert settings.threads == 6
    assert settings.temperature == 0.2


def test_settings_reject_unknown_provider():
    with pytest.raises(ValueError):
        load_settings({"ORION_AI_PROVIDER": "gpt"})


def test_echo_provider_selected_explicitly(tmp_path):
    runtime = build_runtime(AISettings(provider="echo"), tmp_path, tmp_path)

    assert isinstance(runtime.provider, EchoAIProvider)
    assert runtime.status()["provider"] == "echo"
    assert runtime.manager is None


def test_auto_falls_back_to_echo_and_explains_why(tmp_path):
    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)
    status = runtime.status()

    assert isinstance(runtime.provider, EchoAIProvider)
    assert status["provider"] == "echo"
    assert "no llama-server" in status["detail"]
    assert "no .gguf model" in status["detail"]


def test_explicit_llama_without_files_fails_loudly(tmp_path):
    runtime = build_runtime(AISettings(provider="llama"), tmp_path, tmp_path)

    assert isinstance(runtime.provider, UnavailableAIProvider)
    assert runtime.status()["state"] == "error"

    with pytest.raises(AIProviderError, match="Local AI unavailable"):
        AIService(runtime.provider).generate_reply([{"role": "user", "content": "hi"}])


def test_auto_picks_llama_when_binary_and_model_exist(tmp_path):
    make_install(tmp_path)

    runtime = build_runtime(AISettings(provider="auto"), tmp_path, tmp_path)
    status = runtime.status()

    assert runtime.kind == "llama"
    assert status["provider"] == "llama"
    assert status["state"] == "stopped"      # built, not started
    assert status["model"] == "tiny.gguf"


def test_missing_only_model_is_reported(tmp_path):
    make_install(tmp_path, model=False)

    runtime = build_runtime(AISettings(provider="llama"), tmp_path, tmp_path)

    assert "no .gguf model" in runtime.status()["detail"]
    assert "no llama-server" not in runtime.status()["detail"]
