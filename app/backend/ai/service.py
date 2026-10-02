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
