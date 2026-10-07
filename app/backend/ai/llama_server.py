"""Finds, launches and supervises a bundled llama.cpp ``llama-server``.

ORION talks to llama-server over localhost HTTP, so a model crash or
out-of-memory kill never takes the web app (or anyone's login) down with it.
"""

import atexit
import http.client
import logging
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.backend.ai.model_catalog import list_models

logger = logging.getLogger("orion.llama")

LOG_TAIL_BYTES = 2000


class LlamaServerError(RuntimeError):
    """llama-server is missing, failed to start, or stopped responding."""


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def platform_tag() -> str:
    """Folder name for this OS/CPU, e.g. ``windows-x64`` or ``linux-arm64``."""
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(
        sys.platform, sys.platform
    )
    machine = platform.machine().lower()

    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = machine or "unknown"

    return f"{system}-{arch}"


def server_binary_name() -> str:
    return "llama-server.exe" if sys.platform == "win32" else "llama-server"


def find_server_binary(base_dir: Path, override: str | None = None) -> Path | None:
    """Locate llama-server.

    Order: explicit override, then ``runtime/llama/<platform-tag>/`` (searched
    recursively, because release archives usually unpack into a subfolder).
    """
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None

    root = base_dir / "runtime" / "llama" / platform_tag()

    if not root.is_dir():
        return None

    return next(root.rglob(server_binary_name()), None)


def find_model(base_dir: Path, override: str | None = None) -> Path | None:
    """Locate a GGUF model: explicit override, else the first ``models/*.gguf``."""
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None

    models = list_models(base_dir)

    return models[0].path if models else None


# --------------------------------------------------------------------------
# Process manager
# --------------------------------------------------------------------------

def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


class LlamaServerManager:
    """Starts llama-server on a free localhost port and waits until it is ready.

    States: ``stopped`` -> ``loading`` -> ``ready`` (or ``error``).
    """

    def __init__(
        self,
        binary: Path | None = None,
        model: Path | None = None,
        *,
        ctx_size: int = 4096,
        threads: int | None = None,
        startup_timeout: float = 180.0,
        log_path: Path | None = None,
        host: str = "127.0.0.1",
        command_builder: Callable[[int], list[str]] | None = None,
    ):
        if command_builder is None and (binary is None or model is None):
            raise ValueError("binary and model are required without a command_builder.")

        self.binary = binary
        self.model = model
        self.ctx_size = ctx_size
        self.threads = threads
        self.startup_timeout = startup_timeout
        self.log_path = log_path
        self.host = host
        self._command_builder = command_builder

        # llama-server listens on localhost, but any web page open in the
        # user's browser can still send requests there. A random key that only
        # ORION knows makes those requests fail with 401.
        self.api_key = secrets.token_urlsafe(32)

        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._thread: threading.Thread | None = None
        self._port: int | None = None
        self._state = "stopped"
        self._error: str | None = None
        self._generation = 0  # identifies the current launch attempt

        atexit.register(self.stop)

    # -- public state ------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def port(self) -> int | None:
        return self._port

    # -- lifecycle ---------------------------------------------------------

    def build_command(self, port: int) -> list[str]:
        if self._command_builder is not None:
            return self._command_builder(port)

        command = [
            str(self.binary),
            "-m", str(self.model),
            "--host", self.host,
            "--port", str(port),
            "-c", str(self.ctx_size),
            "--parallel", "1",  # one conversation gets the whole context
        ]

        if self.threads:
            command += ["-t", str(self.threads)]

        return command

    def start(self) -> None:
        """Begin launching in the background. Safe to call repeatedly."""
        with self._lock:
            if self._state in ("loading", "ready"):
                return

            self._generation += 1
            self._state = "loading"
            self._error = None
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._launch,
                args=(self._generation,),
                name="llama-server-launch",
                daemon=True,
            )
            self._thread.start()

    def switch_model(self, model: Path) -> None:
        """Stop the running server and start it again on a different model.

        Returns immediately; the new model loads in the background, so watch
        ``state`` (``loading`` -> ``ready`` or ``error``).
        """
        with self._lock:
            self.stop()
            self.model = model
            self.start()

    def wait_ready(self, timeout: float | None = None) -> int:
        """Return the server port once it is ready, (re)starting it if needed."""
        with self._lock:
            if self._state == "ready" and self._process_died():
                logger.warning("llama-server exited unexpectedly; restarting.")
                self._cleanup_process()
                self._state = "stopped"

            if self._state == "stopped":
                self.start()

            if self._state == "error":
                raise LlamaServerError(self._error or "llama-server failed to start.")

        # By default wait a little longer than the launcher's own deadline so
        # its more specific failure message wins the race.
        wait_for = self.startup_timeout + 5 if timeout is None else timeout

        if not self._ready.wait(wait_for):
            raise LlamaServerError(
                f"The AI model is still loading after {wait_for:.0f}s. "
                "Try again in a moment."
            )

        with self._lock:
            if self._state != "ready" or self._port is None:
                raise LlamaServerError(self._error or "llama-server is not ready.")

            return self._port

    def stop(self) -> None:
        with self._lock:
            self._generation += 1  # abandons any launch still in progress
            self._cleanup_process()
            self._state = "stopped"
            self._ready.set()  # release waiters; they will see "not ready"

    # -- internals ---------------------------------------------------------

    def _process_died(self) -> bool:
        return self._process is not None and self._process.poll() is not None

    def _fail(self, generation: int, message: str) -> None:
        with self._lock:
            if generation != self._generation:
                return  # a newer start()/stop() superseded this attempt

            logger.error(message)
            self._cleanup_process()
            self._error = message
            self._state = "error"
            self._ready.set()  # release waiters; they will see the error

    def _launch(self, generation: int) -> None:
        try:
            port = _free_port(self.host)
            command = self.build_command(port)
            cwd = self.binary.parent if self.binary is not None else None

            # Passed through the environment, not --api-key: command lines are
            # visible to every user of the machine, the environment is not.
            kwargs: dict = {"env": {**os.environ, "LLAMA_API_KEY": self.api_key}}

            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_handle = open(self.log_path, "ab")
                kwargs["stdout"] = self._log_handle
                kwargs["stderr"] = subprocess.STDOUT
            else:
                kwargs["stdout"] = subprocess.DEVNULL
                kwargs["stderr"] = subprocess.DEVNULL

            logger.info("Starting llama-server: %s", " ".join(command))

            try:
                process = subprocess.Popen(command, cwd=cwd, **kwargs)
            except PermissionError as exc:
                raise LlamaServerError(
                    f"Not allowed to run {command[0]}. The drive may be mounted "
                    "without execute permission, or security software is "
                    f"blocking it. ({exc})"
                ) from exc
            except OSError as exc:
                raise LlamaServerError(
                    f"Could not start {command[0]}: {exc}. The binary may be "
                    "for a different OS or CPU type."
                ) from exc

            with self._lock:
                if generation != self._generation:
                    process.kill()  # superseded while spawning
                    return

                self._process = process
                self._port = port

            self._wait_until_healthy(generation, process, port)

            with self._lock:
                if generation != self._generation:
                    return

                self._state = "ready"
                self._ready.set()

            logger.info("llama-server ready on port %s", port)

        except LlamaServerError as exc:
            self._fail(generation, str(exc))
        except Exception as exc:  # never let the launcher thread die silently
            logger.exception("Unexpected llama-server launch failure")
            self._fail(generation, f"Unexpected error starting the AI model: {exc}")

    def _wait_until_healthy(
        self,
        generation: int,
        process: subprocess.Popen,
        port: int,
    ) -> None:
        deadline = time.monotonic() + self.startup_timeout

        while time.monotonic() < deadline:
            if generation != self._generation:  # stop()/restart happened
                raise LlamaServerError("Startup was cancelled.")

            exit_code = process.poll()

            if exit_code is not None:
                raise LlamaServerError(
                    f"llama-server exited during startup (code {exit_code}). "
                    f"{self._log_tail()}".strip()
                )

            if self._health_ok(port):
                return

            time.sleep(0.2)

        raise LlamaServerError(
            f"The AI model did not become ready within {self.startup_timeout:.0f}s. "
            "A slow USB drive or too little free RAM are the usual causes."
        )

    def _health_ok(self, port: int) -> bool:
        connection = http.client.HTTPConnection(self.host, port, timeout=2)

        try:
            connection.request(
                "GET",
                "/health",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            return connection.getresponse().status == 200
        except OSError:
            return False  # not listening yet
        finally:
            connection.close()

    def _log_tail(self) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""

        try:
            with open(self.log_path, "rb") as file:
                file.seek(0, os.SEEK_END)
                file.seek(max(0, file.tell() - LOG_TAIL_BYTES))
                tail = file.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

        return f"Last log lines: {tail}" if tail else ""

    def _cleanup_process(self) -> None:
        process, self._process = self._process, None
        handle, self._log_handle = self._log_handle, None

        if process is not None and process.poll() is None:
            process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        if handle is not None:
            handle.close()
