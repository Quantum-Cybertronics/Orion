import re
from collections.abc import Iterator

from app.backend.ai.base import AIProvider, AIProviderError


class EchoAIProvider(AIProvider):
    """Deterministic provider for local development and tests."""

    def generate(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        last_message = messages[-1]["content"]

        return f"ORION received: {last_message}"

    def stream(
        self,
        messages: list[dict[str, str]],
    ) -> Iterator[str]:
        # Word-sized pieces; joining them reproduces generate() exactly.
        yield from re.findall(r"\S+\s*|\s+", self.generate(messages))

    def generate_title(self, content: str) -> str:
        title = " ".join(content.split())

        if len(title) > 80:
            title = title[:80].rstrip()

        return title


class UnavailableAIProvider(AIProvider):
    """Stands in when the configured AI backend cannot be used.

    Every call fails with a readable reason instead of crashing the app.
    """

    def __init__(self, reason: str):
        self.reason = reason

    def generate(self, messages: list[dict[str, str]]) -> str:
        raise AIProviderError(self.reason)

    def stream(self, messages: list[dict[str, str]]) -> Iterator[str]:
        raise AIProviderError(self.reason)
        yield  # pragma: no cover - makes this a generator

    def generate_title(self, content: str) -> str:
        return EchoAIProvider().generate_title(content)
