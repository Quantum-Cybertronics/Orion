"""Start ORION: pick a free port, run the server, open the browser.

Double-click ``Start-ORION.bat`` (Windows) or run ``./start-orion.sh``
(Linux/macOS); both just call this file with the project's own Python.
It can also be run directly:  python launch.py [--port 8000] [--no-browser]
"""

import argparse
import http.client
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
PORT_TRIES = 20          # ports tried: DEFAULT_PORT .. DEFAULT_PORT + 19
START_PAGE = "/login"    # the page a person should land on
BROWSER_WAIT_SECONDS = 120

MISSING_PACKAGES = """\
ORION cannot start because its Python packages are not installed.

Install them once with:
    {python} -m pip install -r requirements.txt
"""


# -- ports and "is it already running?" -------------------------------------

def port_is_free(host: str, port: int) -> bool:
    """True if nothing is listening on (host, port) right now."""
    # No SO_REUSEADDR: on Windows it would let us "bind" a port in use.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False

    return True


def pick_port(host: str, preferred: int, tries: int = PORT_TRIES) -> int:
    """The first free port starting at ``preferred``."""
    for port in range(preferred, min(preferred + tries, 65536)):
        if port_is_free(host, port):
            return port

    raise RuntimeError(
        f"No free port found from {preferred} to {preferred + tries - 1}. "
        "Close other programs, or choose one with --port."
    )


def orion_is_running(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if an ORION server answers on (host, port)."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)

    try:
        connection.request("GET", "/")
        body = connection.getresponse().read(4096)

        return json.loads(body).get("name") == "ORION"
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def find_running_orion(host: str, preferred: int, tries: int = PORT_TRIES) -> int | None:
    """Port of an ORION already running in the usual range, if any."""
    for port in range(preferred, min(preferred + tries, 65536)):
        if orion_is_running(host, port):
            return port

    return None


def wait_until_up(host: str, port: int, timeout: float) -> bool:
    """Wait until something accepts connections on (host, port)."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)

    return False


def open_browser_when_ready(host: str, port: int, url: str) -> None:
    """Open ``url`` once the server is accepting connections (runs in a thread)."""
    if wait_until_up(host, port, BROWSER_WAIT_SECONDS):
        webbrowser.open(url)


# -- what the person sees in the console ------------------------------------

def describe_setup(
    base_dir: Path = BASE_DIR,
    env: Mapping[str, str] = os.environ,
) -> list[str]:
    """Plain-text lines saying whether the AI engine and models are in place.

    ``env`` is where ORION's settings are read from (the real environment by
    default); tests pass their own so they do not depend on the machine.
    """
    from app.backend.ai.llama_server import find_server_binary, platform_tag
    from app.backend.ai.model_catalog import list_models, models_dir
    from app.backend.ai.runtime import load_settings

    try:
        settings = load_settings(env)
    except ValueError as exc:
        return [f"Settings problem: {exc}"]

    if settings.provider == "echo":
        return ["AI        : echo mode (no real AI), set by ORION_AI_PROVIDER"]

    lines = []
    binary = find_server_binary(base_dir, settings.server_path)

    if binary:
        lines.append("AI engine : found")
    else:
        folder = base_dir / "runtime" / "llama" / platform_tag()
        lines.append(f"AI engine : NOT FOUND - add llama.cpp's files to {folder}")

    count = len(list_models(base_dir))

    if count:
        lines.append(f"Models    : {count} found")
    else:
        lines.append(f"Models    : none yet - add .gguf files to {models_dir(base_dir)}")

    if not binary or not count:
        lines.append("            (ORION shows these steps in the browser too)")

    return lines


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start ORION.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="preferred port")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )

    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    opener: Callable[[str, int, str], None] = open_browser_when_ready,
) -> int:
    args = parse_args(argv)
    host = args.host

    # A second double-click should reopen ORION, not start a rival copy that
    # fights over the same database and model.
    running = find_running_orion(host, args.port)

    if running is not None:
        url = f"http://{host}:{running}{START_PAGE}"

        print(f"ORION is already running. Opening {url}")

        if not args.no_browser:
            webbrowser.open(url)

        return 0

    try:
        port = pick_port(host, args.port)
    except RuntimeError as exc:
        print(exc)
        return 1

    try:
        import uvicorn
    except ImportError:
        print(MISSING_PACKAGES.format(python=sys.executable))
        return 1

    url = f"http://{host}:{port}{START_PAGE}"

    print()
    print("  ORION")
    print("  -----")

    if port != args.port:
        print(f"  Port {args.port} is busy, so ORION is using {port}.")

    print(f"  Address   : {url}")

    try:
        for line in describe_setup():
            print(f"  {line}")
    except Exception as exc:  # hints must never stop ORION from starting
        print(f"  (could not check the AI setup: {exc})")

    print()
    print("  Keep this window open while you use ORION.")
    print("  Press Ctrl+C here to stop it.")
    print()

    if not args.no_browser:
        threading.Thread(target=opener, args=(host, port, url), daemon=True).start()

    uvicorn.run("app.backend.main:app", host=host, port=port, log_level="info")

    return 0


if __name__ == "__main__":
    os.chdir(BASE_DIR)  # migrations and relative paths expect the project root
    sys.exit(main())
