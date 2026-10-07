"""Chooses and owns the AI backend for the running app.

ORION_AI_PROVIDER:
  auto  (default) use the bundled llama-server if a binary and model are
        found, otherwise fall back to the echo stand-in and say why.
  llama require llama-server; if it cannot be used, replies fail with a clear
        reason instead of silently echoing.
  echo  always use the echo stand-in (tests, UI development).
"""

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.backend.ai.base import AIProvider
from app.backend.ai.llama_provider import LlamaServerProvider
from app.backend.ai.llama_server import (
    LlamaServerManager,
    find_model,
    find_server_binary,
    platform_tag,
)
from app.backend.ai.model_catalog import (
    ModelInfo,
    choose_initial_model,
    describe_model,
    list_models,
    save_selection,
)
from app.backend.ai.providers import EchoAIProvider, UnavailableAIProvider
from app.backend.ai.service import AIService
from app.backend.ai.tokens import FileBudget, file_budget
from app.backend.config import BASE_DIR, DATA_DIR

logger = logging.getLogger("orion.ai")


@dataclass(frozen=True)
class AISettings:
    provider: str = "auto"
    ctx_size: int = 8192
    max_tokens: int = 1024
    temperature: float = 0.7
    threads: int | None = None
    startup_timeout: float = 180.0
    server_path: str | None = None
    model_path: str | None = None


def load_settings(env: Mapping[str, str] = os.environ) -> AISettings:
    provider = (env.get("ORION_AI_PROVIDER") or "auto").strip().lower()

    if provider not in ("auto", "llama", "echo"):
        raise ValueError(
            f"ORION_AI_PROVIDER must be auto, llama or echo (got {provider!r})."
        )

    def number(name: str, default, cast):
        raw = env.get(name)
        return cast(raw) if raw else default

    return AISettings(
        provider=provider,
        ctx_size=number("ORION_LLAMA_CTX", 8192, int),
        max_tokens=number("ORION_MAX_REPLY_TOKENS", 1024, int),
        temperature=number("ORION_TEMPERATURE", 0.7, float),
        threads=number("ORION_LLAMA_THREADS", None, int),
        startup_timeout=number("ORION_LLAMA_STARTUP_TIMEOUT", 180.0, float),
        server_path=env.get("ORION_LLAMA_SERVER") or None,
        model_path=env.get("ORION_MODEL_PATH") or None,
    )


class ModelSwitchError(RuntimeError):
    """The requested model change cannot happen (message is safe to show).

    ``reason`` is one of: ``unavailable``, ``locked``, ``not_found``, ``busy``.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _make_llama(
    settings: AISettings,
    binary: Path,
    model: Path,
    data_dir: Path,
) -> tuple[LlamaServerManager, LlamaServerProvider]:
    manager = LlamaServerManager(
        binary,
        model,
        ctx_size=settings.ctx_size,
        threads=settings.threads,
        startup_timeout=settings.startup_timeout,
        log_path=data_dir / "llama-server.log",
    )

    provider = LlamaServerProvider(
        manager,
        ctx_size=settings.ctx_size,
        max_tokens=settings.max_tokens,
        temperature=settings.temperature,
    )

    return manager, provider


class AIRuntime:
    def __init__(
        self,
        provider: AIProvider,
        kind: str,
        manager: LlamaServerManager | None = None,
        model: Path | None = None,
        note: str | None = None,
        settings: AISettings | None = None,
        *,
        binary: Path | None = None,
        base_dir: Path = BASE_DIR,
        data_dir: Path = DATA_DIR,
        model_locked: bool = False,
    ):
        self.provider = provider
        self.kind = kind  # "llama" | "echo" | "unavailable"
        self.manager = manager
        self.model = model
        self.note = note
        self.service = AIService(provider)
        self.settings = settings or AISettings()

        self.binary = binary
        self.base_dir = base_dir
        self.data_dir = data_dir
        self.model_locked = model_locked  # pinned by ORION_MODEL_PATH

        self._switch_lock = threading.Lock()

    def file_budget(self) -> FileBudget:
        """How much attached-file text fits in the model's context window."""
        return file_budget(self.settings.ctx_size, self.settings.max_tokens)

    def start(self) -> None:
        """Begin loading the model in the background (does not block)."""
        if self.manager is not None:
            self.manager.start()

    def stop(self) -> None:
        if self.manager is not None:
            self.manager.stop()

    # -- model selection ---------------------------------------------------

    @property
    def busy(self) -> bool:
        """True while a reply is being generated."""
        return bool(getattr(self.provider, "busy", False))

    @property
    def can_switch(self) -> bool:
        return (
            self.settings.provider != "echo"
            and self.binary is not None
            and not self.model_locked
        )

    def available_models(self) -> list[ModelInfo]:
        if self.model_locked and self.model is not None:
            return [describe_model(self.model)]

        return list_models(self.base_dir)

    def select_model(self, model_id: str) -> dict:
        """Switch to the named model from ``models/`` and return the new status.

        The new model loads in the background; the status shows ``loading``
        until it is ready. The choice is remembered across restarts.
        """
        if self.settings.provider == "echo" or self.binary is None:
            raise ModelSwitchError(
                "unavailable",
                "The local AI engine is not available, so models cannot be switched.",
            )

        if self.model_locked:
            raise ModelSwitchError(
                "locked",
                "The model is fixed by the ORION_MODEL_PATH setting.",
            )

        chosen = next(
            (m for m in list_models(self.base_dir) if m.id == model_id), None
        )

        if chosen is None:
            raise ModelSwitchError(
                "not_found",
                f"Model {model_id!r} was not found in the models folder.",
            )

        with self._switch_lock:
            already_running = (
                self.manager is not None
                and self.model == chosen.path
                and self.manager.state in ("loading", "ready")
            )

            if already_running:
                return self.status()

            if self.busy:
                raise ModelSwitchError(
                    "busy",
                    "ORION is writing a reply right now. "
                    "Wait for it to finish, then switch models.",
                )

            if self.manager is None:
                self._attach_llama(chosen.path)
            else:
                self.manager.switch_model(chosen.path)

            self.model = chosen.path
            self.note = None

        save_selection(self.data_dir, chosen.id)
        logger.info("Switched model to %s", chosen.id)

        return self.status()

    def _attach_llama(self, model: Path) -> None:
        """Bring up llama-server for a runtime that started without a model."""
        manager, provider = _make_llama(
            self.settings, self.binary, model, self.data_dir
        )

        self.manager = manager
        self.provider = provider
        self.service = AIService(provider)
        self.kind = "llama"
        manager.start()

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        if self.kind == "llama" and self.manager is not None:
            info = {
                "provider": "llama",
                "state": self.manager.state,
                "detail": self.manager.error,
                "model": self.model.name if self.model else None,
            }
        elif self.kind == "unavailable":
            info = {
                "provider": "unavailable",
                "state": "error",
                "detail": self.note,
                "model": None,
            }
        else:
            info = {
                "provider": "echo",
                "state": "ready",
                "detail": self.note,
                "model": None,
            }

        info["models"] = [m.to_dict() for m in self.available_models()]
        info["can_switch"] = self.can_switch

        return info


def build_runtime(
    settings: AISettings,
    base_dir: Path = BASE_DIR,
    data_dir: Path = DATA_DIR,
) -> AIRuntime:
    common = {"settings": settings, "base_dir": base_dir, "data_dir": data_dir}

    if settings.provider == "echo":
        return AIRuntime(
            EchoAIProvider(), "echo", note="Echo mode (no AI model).", **common
        )

    binary = find_server_binary(base_dir, settings.server_path)

    if settings.model_path:
        # An explicit path pins the model; the dropdown cannot change it.
        model = find_model(base_dir, settings.model_path)
        pinned = model is not None
    else:
        model = choose_initial_model(base_dir, data_dir)
        pinned = False

    missing = []

    if binary is None:
        missing.append(
            f"no llama-server found (expected under runtime/llama/{platform_tag()}/)"
        )

    if model is None:
        missing.append("no .gguf model found (expected in models/)")

    if missing:
        reason = "Local AI unavailable: " + "; ".join(missing) + "."

        if settings.provider == "llama":
            logger.error(reason)
            return AIRuntime(
                UnavailableAIProvider(reason),
                "unavailable",
                note=reason,
                binary=binary,
                **common,
            )

        logger.warning("%s Falling back to echo mode.", reason)
        return AIRuntime(
            EchoAIProvider(),
            "echo",
            note=f"Echo mode. {reason}",
            binary=binary,
            **common,
        )

    manager, provider = _make_llama(settings, binary, model, data_dir)

    return AIRuntime(
        provider,
        "llama",
        manager=manager,
        model=model,
        binary=binary,
        model_locked=pinned,
        **common,
    )


_runtime: AIRuntime | None = None


def get_runtime() -> AIRuntime:
    global _runtime

    if _runtime is None:
        _runtime = build_runtime(load_settings())

    return _runtime


def reset_runtime() -> None:
    """Stop and forget the cached runtime (used by tests)."""
    global _runtime

    if _runtime is not None:
        _runtime.stop()

    _runtime = None


def get_ai_service() -> AIService:
    return get_runtime().service
