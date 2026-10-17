import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

import launch
from app.backend.ai.llama_server import platform_tag, server_binary_name

HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


class FakeServer:
    """A tiny HTTP server answering GET / with the given JSON."""

    def __init__(self, payload):
        body = json.dumps(payload).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer((HOST, 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ------------------------------------------------------------------ ports

def test_pick_port_uses_the_preferred_port_when_free():
    port = free_port()

    assert launch.pick_port(HOST, port) == port


def test_pick_port_skips_a_busy_port():
    with socket.socket() as busy:
        busy.bind((HOST, 0))
        busy_port = busy.getsockname()[1]

        picked = launch.pick_port(HOST, busy_port)

    assert picked > busy_port


def test_pick_port_gives_a_clear_error_when_nothing_is_free():
    with socket.socket() as busy:
        busy.bind((HOST, 0))

        with pytest.raises(RuntimeError) as error:
            launch.pick_port(HOST, busy.getsockname()[1], tries=1)

    assert "No free port" in str(error.value)


# ------------------------------------------------------ "already running?"

def test_recognises_a_running_orion():
    server = FakeServer({"name": "ORION", "status": "online"})

    try:
        assert launch.orion_is_running(HOST, server.port) is True
        assert launch.find_running_orion(HOST, server.port, tries=3) == server.port
    finally:
        server.close()


def test_a_different_program_on_the_port_is_not_orion():
    server = FakeServer({"name": "something else"})

    try:
        assert launch.orion_is_running(HOST, server.port) is False
        assert launch.find_running_orion(HOST, server.port, tries=1) is None
    finally:
        server.close()


def test_nothing_listening_is_not_orion():
    assert launch.orion_is_running(HOST, free_port()) is False


def test_wait_until_up():
    server = FakeServer({"name": "ORION"})

    try:
        assert launch.wait_until_up(HOST, server.port, timeout=2) is True
    finally:
        server.close()

    assert launch.wait_until_up(HOST, free_port(), timeout=0.6) is False


# --------------------------------------------------------------- main()

@pytest.fixture
def fake_uvicorn(monkeypatch):
    calls = []
    module = SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "uvicorn", module)
    return calls


def test_main_starts_the_server_and_opens_the_browser(fake_uvicorn, capsys):
    port = free_port()
    opened = []

    code = launch.main(
        ["--port", str(port)],
        opener=lambda host, p, url: opened.append((host, p, url)),
    )

    assert code == 0
    (args, kwargs), = fake_uvicorn
    assert args == ("app.backend.main:app",)
    assert kwargs["host"] == HOST and kwargs["port"] == port

    # the opener runs on a thread; give it a moment
    for _ in range(50):
        if opened:
            break
        threading.Event().wait(0.02)

    assert opened == [(HOST, port, f"http://{HOST}:{port}/login")]
    assert f"http://{HOST}:{port}/login" in capsys.readouterr().out


def test_main_uses_another_port_when_the_first_is_busy(fake_uvicorn, capsys):
    with socket.socket() as busy:
        busy.bind((HOST, 0))
        busy_port = busy.getsockname()[1]

        launch.main(["--port", str(busy_port), "--no-browser"])

    (_, kwargs), = fake_uvicorn

    assert kwargs["port"] > busy_port
    assert "is busy" in capsys.readouterr().out


def test_no_browser_flag(fake_uvicorn):
    opened = []

    launch.main(["--port", str(free_port()), "--no-browser"], opener=lambda *a: opened.append(a))
    threading.Event().wait(0.1)

    assert opened == []


def test_a_second_launch_reopens_the_running_orion(fake_uvicorn, monkeypatch, capsys):
    server = FakeServer({"name": "ORION"})
    browser = []
    monkeypatch.setattr(launch.webbrowser, "open", lambda url: browser.append(url))

    try:
        code = launch.main(["--port", str(server.port)])
    finally:
        server.close()

    assert code == 0
    assert fake_uvicorn == []                                   # no rival server
    assert browser == [f"http://{HOST}:{server.port}/login"]
    assert "already running" in capsys.readouterr().out


def test_missing_packages_give_install_instructions(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # makes `import uvicorn` fail

    code = launch.main(["--port", str(free_port()), "--no-browser"])
    out = capsys.readouterr().out

    assert code == 1
    assert "pip install -r requirements.txt" in out


# ------------------------------------------------------------ console hints

def test_setup_lines_for_an_empty_install(tmp_path):
    lines = "\n".join(launch.describe_setup(tmp_path, env={}))

    assert "NOT FOUND" in lines and str(tmp_path / "runtime") in lines
    assert "none yet" in lines and str(tmp_path / "models") in lines


def test_setup_lines_for_a_complete_install(tmp_path):
    folder = tmp_path / "runtime" / "llama" / platform_tag()
    folder.mkdir(parents=True)
    (folder / server_binary_name()).write_bytes(b"x")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.gguf").write_bytes(b"x")
    (tmp_path / "models" / "b.gguf").write_bytes(b"x")

    lines = launch.describe_setup(tmp_path, env={})

    assert "AI engine : found" in lines
    assert "Models    : 2 found" in lines
    assert not any("browser" in line for line in lines)


def test_setup_lines_say_so_when_echo_mode_is_chosen(tmp_path):
    lines = launch.describe_setup(tmp_path, env={"ORION_AI_PROVIDER": "echo"})

    assert len(lines) == 1 and "echo mode" in lines[0]


def test_setup_lines_report_a_bad_setting_instead_of_crashing(tmp_path):
    lines = launch.describe_setup(tmp_path, env={"ORION_AI_PROVIDER": "banana"})

    assert len(lines) == 1 and lines[0].startswith("Settings problem")
