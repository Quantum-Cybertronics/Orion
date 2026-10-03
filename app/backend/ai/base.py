from abc import ABC, abstractmethod
from collections.abc import Iterator


class AIProviderError(RuntimeError):
    """The AI backend could not produce a reply (message is safe to show)."""


class AIProvider(ABC):
    @abstractmethod
    def generate(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        """Generate an assistant response from conversation messages."""
        raise NotImplementedError

    def stream(
        self,
        messages: list[dict[str, str]],
    ) -> Iterator[str]:
        """Yield the assistant response in pieces as it is produced.

        Providers that can stream should override this. The default simply
        yields the complete response as a single piece.
        """
        yield self.generate(messages)

    @abstractmethod
    def generate_title(
        self,
        content: str,
    ) -> str:
        """Generate a concise conversation title from the first user message."""
        raise NotImplementedError
