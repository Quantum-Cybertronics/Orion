import pytest

from app.backend.ai.base import AIProvider
from app.backend.ai.service import AIService
from app.backend.ai.providers import EchoAIProvider


class FakeAIProvider(AIProvider):
    def __init__(self):
        self.received_messages = None

    def generate(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        self.received_messages = messages
        return "Hello from the test provider."


def test_ai_provider_interface_can_generate():
    provider = FakeAIProvider()

    result = provider.generate(
        [
            {
                "role": "user",
                "content": "Hello",
            }
        ]
    )

    assert result == "Hello from the test provider."


def test_ai_service_delegates_to_provider():
    provider = FakeAIProvider()
    service = AIService(provider)

    messages = [
        {
            "role": "user",
            "content": "Hello ORION",
        }
    ]

    result = service.generate_reply(messages)

    assert result == "Hello from the test provider."
    assert provider.received_messages == messages


def test_ai_service_rejects_empty_messages():
    provider = FakeAIProvider()
    service = AIService(provider)

    with pytest.raises(ValueError):
        service.generate_reply([])

def test_echo_provider_returns_last_user_message():
    provider = EchoAIProvider()

    result = provider.generate(
        [
            {
                "role": "user",
                "content": "What can you do?",
            }
        ]
    )

    assert result == "ORION received: What can you do?"
