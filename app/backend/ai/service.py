from collections.abc import Iterator

from app.backend.ai.base import AIProvider


class AIService:
    def __init__(self, provider: AIProvider):
        self.provider = provider

    def generate_reply(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        if not messages:
            raise ValueError("Messages cannot be empty.")

        return self.provider.generate(messages)

    def stream_reply(
        self,
        messages: list[dict[str, str]],
    ) -> Iterator[str]:
        if not messages:
            raise ValueError("Messages cannot be empty.")

        return self.provider.stream(messages)

    def generate_title(
        self,
        content: str,
    ) -> str:
        if not content.strip():
            raise ValueError("Content cannot be empty.")

        return self.provider.generate_title(content)
