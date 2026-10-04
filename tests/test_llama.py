import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backend.ai import llama_server
from app.backend.ai.base import AIProviderError
from app.backend.ai.llama_provider import (
    LlamaServerProvider,
    describe_today,
    estimate_tokens,
    fit_to_context,
)
from app.backend.ai.llama_server import (
    LlamaServerError,
    LlamaServerManager,
    find_model,
    find_server_binary,
    platform_tag,
)

FAKE_SERVER = Path(__file__).with_name("fake_llama_server.py")


def fake_builder(startup_delay: float = 0.0):
    def build(port: int) -> list[str]:
        return [
            sys.executable,
            str(FAKE_SERVER),
            "--port", str(port),
            "--startup-delay", str(startup_delay),
        ]

    return build


@pytest.fixture
def manager():
    created = []

    def make(**kwargs) -> LlamaServerManager:
        kwargs.setdefault("command_builder", fake_builder())
        kwargs.setdefault("startup_timeout", 15)
        instance = LlamaServerManager(**kwargs)
        created.append(instance)
        return instance

    yield make

    for instance in created:
        instance.stop()


@pytest.fixture
def provider(manager):
    return LlamaServerProvider(manager(), ctx_size=4096, max_tokens=256)


# ---------------------------------------------------------------- discovery

def test_platform_tag_maps_os_and_cpu(monkeypatch):
    monkeypatch.setattr(llama_server.sys, "platform", "win32")
    monkeypatch.setattr(llama_server.platform, "machine", lambda: "AMD64")
    assert platform_tag() == "windows-x64"

    monkeypatch.setattr(llama_server.sys, "platform", "linux")
    monkeypatch.setattr(llama_server.platform, "machine", lambda: "aarch64")
    assert platform_tag() == "linux-arm64"


def test_find_server_binary_searches_platform_folder(tmp_path):
    nested = tmp_path / "runtime" / "llama" / platform_tag() / "build" / "bin"
    nested.mkdir(parents=True)
    binary = nested / llama_server.server_binary_name()
    binary.write_bytes(b"x")

    assert find_server_binary(tmp_path) == binary


def test_find_server_binary_missing_and_override(tmp_path):
    assert find_server_binary(tmp_path) is None

    custom = tmp_path / "custom-server"
    custom.write_bytes(b"x")

    assert find_server_binary(tmp_path, str(custom)) == custom
    assert find_server_binary(tmp_path, str(tmp_path / "nope")) is None


def test_find_model_picks_first_gguf(tmp_path):
    assert find_model(tmp_path) is None

    models = tmp_path / "models"
    models.mkdir()
    (models / "b.gguf").write_bytes(b"x")
    (models / "a.gguf").write_bytes(b"x")
    (models / "notes.txt").write_bytes(b"x")

    assert find_model(tmp_path).name == "a.gguf"


# ------------------------------------------------------------------ manager

def test_manager_starts_and_becomes_ready(manager):
    server = manager(command_builder=fake_builder(startup_delay=0.5))

    assert server.state == "stopped"
    port = server.wait_ready()

    assert server.state == "ready"
    assert port == server.port


def test_manager_stop_terminates_process(manager):
    server = manager()
    server.wait_ready()
    process = server._process

    server.stop()

    assert process.poll() is not None
    assert server.state == "stopped"


def test_manager_restarts_after_crash(manager):
    server = manager()
    first_port = server.wait_ready()
    server._process.kill()
    server._process.wait()

    second_port = server.wait_ready()

    assert server.state == "ready"
    assert second_port != first_port


def test_manager_reports_unrunnable_binary(manager, tmp_path):
    server = manager(
        command_builder=lambda port: [str(tmp_path / "does-not-exist")],
    )

    with pytest.raises(LlamaServerError, match="Could not start"):
        server.wait_ready()

    assert server.state == "error"


def test_manager_reports_early_exit_with_log(manager, tmp_path):
    log = tmp_path / "llama.log"
    server = manager(
        command_builder=lambda port: [
            sys.executable, "-c", "print('boom: model file is corrupt'); raise SystemExit(3)",
        ],
        log_path=log,
    )

    with pytest.raises(LlamaServerError) as error:
        server.wait_ready()

    assert "exited during startup" in str(error.value)
    assert "model file is corrupt" in str(error.value)


def test_manager_times_out_if_never_healthy(manager):
    server = manager(
        command_builder=fake_builder(startup_delay=30),
        startup_timeout=1,
    )

    with pytest.raises(LlamaServerError, match="did not become ready"):
        server.wait_ready()


def test_manager_requires_binary_and_model_without_builder():
    with pytest.raises(ValueError):
        LlamaServerManager()


def test_default_command_binds_localhost_only(tmp_path):
    server = LlamaServerManager(tmp_path / "llama-server", tmp_path / "m.gguf", ctx_size=2048, threads=4)
    command = server.build_command(5555)

    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "5555"
    assert command[command.index("-c") + 1] == "2048"
    assert command[command.index("-t") + 1] == "4"


# ----------------------------------------------------------------- provider

def test_provider_streams_pieces_that_join_to_the_reply(provider):
    pieces = list(provider.stream([{"role": "user", "content": "hello there"}]))

    assert len(pieces) > 1
    # system prompt is added, so the server sees 2 messages
    assert "".join(pieces) == "echo[2]: hello there"


def test_provider_generate_returns_full_text(provider):
    assert provider.generate([{"role": "user", "content": "hi"}]) == "echo[2]: hi"


def test_provider_surfaces_http_errors(provider):
    with pytest.raises(AIProviderError, match="context size"):
        provider.generate([{"role": "user", "content": "__error__"}])


def test_provider_reports_unavailable_model(manager, tmp_path):
    broken = manager(command_builder=lambda port: [str(tmp_path / "nope")])
    provider = LlamaServerProvider(broken)

    with pytest.raises(AIProviderError, match="Could not start"):
        provider.generate([{"role": "user", "content": "hi"}])


def test_provider_title_is_instant_and_short(provider):
    assert provider.generate_title("  Hello \n world ") == "Hello world"
    assert len(provider.generate_title("x" * 200)) == 60


def test_provider_keeps_existing_system_prompt(manager):
    provider = LlamaServerProvider(manager())
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
    ]

    assert provider.generate(messages) == "echo[2]: hi"


def test_caller_system_messages_are_merged_after_orions_own(manager):
    provider = LlamaServerProvider(manager(), clock=lambda: FIXED_NOW)
    messages = [
        {"role": "system", "content": "FILE ONE TEXT"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]

    prepared = provider._prepare(messages)
    system = [m for m in prepared if m["role"] == "system"]

    assert len(system) == 1
    assert system[0]["content"].startswith("You are ORION")
    assert system[0]["content"].endswith("FILE ONE TEXT")
    assert [m["role"] for m in prepared] == ["system", "user", "assistant", "user"]


def test_attached_file_text_survives_history_trimming(manager):
    provider = LlamaServerProvider(manager(), ctx_size=1500, max_tokens=200)
    history = [{"role": "system", "content": "THE-STORY " * 100}]

    for index in range(30):
        history.append({"role": "user", "content": f"q{index} " + "x" * 150})
        history.append({"role": "assistant", "content": "a " + "y" * 150})

    history.append({"role": "user", "content": "latest question"})

    prepared = provider._prepare(history)

    assert "THE-STORY" in prepared[0]["content"]
    assert prepared[-1]["content"] == "latest question"
    assert len(prepared) < len(history)


def test_provider_trims_old_history_to_fit_context(manager):
    provider = LlamaServerProvider(manager(), ctx_size=600, max_tokens=200)
    history = []

    for index in range(40):
        history.append({"role": "user", "content": f"question {index} " + "x" * 200})
        history.append({"role": "assistant", "content": "answer " + "y" * 200})

    history.append({"role": "user", "content": "latest"})

    reply = provider.generate(history)
    count = int(reply.split("[")[1].split("]")[0])

    assert count < len(history)          # something was dropped
    assert reply.endswith("latest")      # newest message always survives


# ------------------------------------------------------------- fit_to_context

def message(role, size):
    return {"role": role, "content": "z" * size}


def test_fit_keeps_everything_when_it_fits():
    messages = [message("system", 30), message("user", 30), message("assistant", 30)]

    assert fit_to_context(messages, 10_000) == messages


def test_fit_drops_oldest_first_and_keeps_system():
    messages = [
        message("system", 30),
        message("user", 300),
        message("assistant", 300),
        message("user", 300),
        message("assistant", 300),
        message("user", 30),
    ]

    fitted = fit_to_context(messages, 220)

    assert fitted[0]["role"] == "system"
    assert fitted[-1] == messages[-1]
    assert len(fitted) < len(messages)


def test_fit_never_starts_with_assistant_turn():
    messages = [message("user", 300), message("assistant", 30), message("user", 30)]

    fitted = fit_to_context(messages, estimate_tokens("z" * 60) * 2 + 40)

    assert fitted[0]["role"] == "user"


def test_fit_always_keeps_newest_message_even_if_too_big():
    messages = [message("user", 10), message("user", 100_000)]

    assert fit_to_context(messages, 50) == [messages[-1]]


# ------------------------------------------------------------ system prompt

FIXED_NOW = datetime(2026, 10, 3, 14, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def test_describe_today_is_date_only_with_offset():
    assert describe_today(FIXED_NOW) == "Saturday, 03 October 2026 (UTC+05:30)"


def test_system_prompt_has_date_and_no_live_data_notice(manager):
    provider = LlamaServerProvider(manager(), clock=lambda: FIXED_NOW)

    prepared = provider._prepare([{"role": "user", "content": "what day is it?"}])

    assert prepared[0]["role"] == "system"
    assert "Saturday, 03 October 2026 (UTC+05:30)" in prepared[0]["content"]
    assert "14:05" not in prepared[0]["content"]   # time of day would break prompt caching
    assert "no internet access" in prepared[0]["content"]
    assert "time of day" in prepared[0]["content"]
    assert prepared[1] == {"role": "user", "content": "what day is it?"}


def test_clock_is_read_per_request(manager):
    times = iter([FIXED_NOW, FIXED_NOW + timedelta(days=1)])
    provider = LlamaServerProvider(manager(), clock=lambda: next(times))
    message = [{"role": "user", "content": "hi"}]

    first = provider._prepare(message)[0]["content"]
    second = provider._prepare(message)[0]["content"]

    assert "03 October" in first and "04 October" in second
