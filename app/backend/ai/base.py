from abc import ABC, abstractmethod


class AIProvider(ABC):
    @abstractmethod
    def generate(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        """Generate an assistant response from conversation messages."""
        raise NotImplementedError
