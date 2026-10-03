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
from app.backend.ai.providers import EchoAIProvider, UnavailableAIProvider
from app.backend.ai.service import AIService
from app.backend.config import BASE_DIR, DATA_DIR

logger = logging.getLogger("orion.ai")


@dataclass(frozen=True)
class AISettings:
    provider: str = "auto"
    ctx_size: int = 4096
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
        ctx_size=number("ORION_LLAMA_CTX", 4096, int),
        max_tokens=number("ORION_MAX_REPLY_TOKENS", 1024, int),
        temperature=number("ORION_TEMPERATURE", 0.7, float),
        threads=number("ORION_LLAMA_THREADS", None, int),
        startup_timeout=number("ORION_LLAMA_STARTUP_TIMEOUT", 180.0, float),
        server_path=env.get("ORION_LLAMA_SERVER") or None,
        model_path=env.get("ORION_MODEL_PATH") or None,
    )


class AIRuntime:
    def __init__(
        self,
        provider: AIProvider,
        kind: str,
        manager: LlamaServerManager | None = None,
        model: Path | None = None,
        note: str | None = None,
    ):
        self.provider = provider
        self.kind = kind  # "llama" | "echo" | "unavailable"
        self.manager = manager
        self.model = model
        self.note = note
        self.service = AIService(provider)

    def start(self) -> None:
        """Begin loading the model in the background (does not block)."""
        if self.manager is not None:
            self.manager.start()

    def stop(self) -> None:
        if self.manager is not None:
            self.manager.stop()

    def status(self) -> dict:
        if self.kind == "llama" and self.manager is not None:
            return {
                "provider": "llama",
                "state": self.manager.state,
                "detail": self.manager.error,
                "model": self.model.name if self.model else None,
            }

        if self.kind == "unavailable":
            return {
                "provider": "unavailable",
                "state": "error",
                "detail": self.note,
                "model": None,
            }

        return {
            "provider": "echo",
            "state": "ready",
            "detail": self.note,
            "model": None,
        }


def build_runtime(
    settings: AISettings,
    base_dir: Path = BASE_DIR,
    data_dir: Path = DATA_DIR,
) -> AIRuntime:
    if settings.provider == "echo":
        return AIRuntime(EchoAIProvider(), "echo", note="Echo mode (no AI model).")

    binary = find_server_binary(base_dir, settings.server_path)
    model = find_model(base_dir, settings.model_path)

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
            return AIRuntime(UnavailableAIProvider(reason), "unavailable", note=reason)

        logger.warning("%s Falling back to echo mode.", reason)
        return AIRuntime(EchoAIProvider(), "echo", note=f"Echo mode. {reason}")

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

    return AIRuntime(provider, "llama", manager=manager, model=model)


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
