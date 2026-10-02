from app.backend.ai.base import AIProvider


class EchoAIProvider(AIProvider):
    """Deterministic provider for local development."""

    def generate(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        last_message = messages[-1]["content"]

        return f"ORION received: {last_message}"
