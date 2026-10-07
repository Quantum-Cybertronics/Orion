"""AI provider that talks to a local llama.cpp ``llama-server``.

llama-server exposes an OpenAI-compatible chat API, so the HTTP/SSE handling
here will also fit the OpenRouter provider later.
"""

import http.client
import json
import threading
from collections.abc import Callable, Iterator
from datetime import datetime

from app.backend.ai.base import AIProvider, AIProviderError
from app.backend.ai.llama_server import LlamaServerError, LlamaServerManager
from app.backend.ai.tokens import estimate_tokens

DEFAULT_SYSTEM_PROMPT = (
    "You are ORION, a helpful, honest and private AI assistant running "
    "locally on the user's own device. Answer clearly and concisely."
)

NO_LIVE_DATA_NOTICE = (
    "You have no internet access and no live data. You cannot know the "
    "current weather, news, prices, sports scores or events after your "
    "training. If asked, say you cannot check instead of guessing. You also "
    "do not know the user's location or time zone unless they tell you."
)


def describe_today(now: datetime) -> str:
    """e.g. ``Saturday, 03 October 2026 (UTC+05:30)``.

    Deliberately date-only: the time of day would change the start of the
    prompt every minute and force the model to re-read any attached files
    on every single message.
    """
    offset = now.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else "local time"

    return f"{now:%A, %d %B %Y} (UTC{offset})"

# Per-message overhead added by chat templates (role markers etc.).
MESSAGE_OVERHEAD_TOKENS = 8


def fit_to_context(
    messages: list[dict[str, str]],
    budget_tokens: int,
) -> list[dict[str, str]]:
    """Drop the oldest turns so the conversation fits in ``budget_tokens``.

    Always keeps system messages and the newest message, and never starts the
    kept conversation with an assistant turn (some chat templates reject it).
    """
    system = [m for m in messages if m["role"] == "system"]
    turns = [m for m in messages if m["role"] != "system"]

    def cost(message: dict[str, str]) -> int:
        return estimate_tokens(message["content"]) + MESSAGE_OVERHEAD_TOKENS

    remaining = budget_tokens - sum(cost(m) for m in system)
    kept: list[dict[str, str]] = []

    for message in reversed(turns):
        if kept and cost(message) > remaining:
            break

        kept.append(message)
        remaining -= cost(message)

    kept.reverse()

    while len(kept) > 1 and kept[0]["role"] == "assistant":
        kept.pop(0)

    return system + kept


class LlamaServerProvider(AIProvider):
    def __init__(
        self,
        manager: LlamaServerManager,
        *,
        ctx_size: int = 4096,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        request_timeout: float = 600.0,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.manager = manager
        self.ctx_size = ctx_size
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.request_timeout = request_timeout
        self._clock = clock

        # One model, one generation at a time; others queue here.
        self._generation_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        """True while a reply is being generated."""
        return self._generation_lock.locked()

    # -- AIProvider --------------------------------------------------------

    def generate(self, messages: list[dict[str, str]]) -> str:
        return "".join(self.stream(messages))

    def stream(self, messages: list[dict[str, str]]) -> Iterator[str]:
        prepared = self._prepare(messages)

        try:
            port = self.manager.wait_ready()
        except LlamaServerError as exc:
            raise AIProviderError(str(exc)) from exc

        with self._generation_lock:
            yield from self._stream_completion(port, prepared)

    def generate_title(self, content: str) -> str:
        # Instant, no model call: a CPU model would delay the first reply.
        title = " ".join(content.split())

        if len(title) > 60:
            title = title[:57].rstrip() + "..."

        return title

    # -- internals ---------------------------------------------------------

    def _prepare(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        # ORION's own prompt always comes first; any system messages the
        # caller supplied (e.g. attached-file text) are merged in after it.
        extra_system = [m["content"] for m in messages if m["role"] == "system"]
        turns = [m for m in messages if m["role"] != "system"]

        system_text = (
            f"{self.system_prompt} "
            f"Today's date on this device is {describe_today(self._clock())}. "
            f"You do not know the current time of day. {NO_LIVE_DATA_NOTICE}"
        )

        if extra_system:
            system_text += "\n\n" + "\n\n".join(extra_system)

        prepared = [{"role": "system", "content": system_text}] + turns
        reply_reserve = min(self.max_tokens, self.ctx_size // 2)

        return fit_to_context(prepared, self.ctx_size - reply_reserve)

    def _stream_completion(
        self,
        port: int,
        messages: list[dict[str, str]],
    ) -> Iterator[str]:
        body = json.dumps(
            {
                "messages": messages,
                "stream": True,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        )

        connection = http.client.HTTPConnection(
            self.manager.host, port, timeout=self.request_timeout
        )

        try:
            try:
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.manager.api_key}",
                    },
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                raise AIProviderError(
                    f"Could not reach the local AI model: {exc}"
                ) from exc

            if response.status != 200:
                raise AIProviderError(_describe_http_error(response))

            try:
                yield from _parse_sse(response)
            except (OSError, http.client.HTTPException) as exc:
                raise AIProviderError(
                    f"The local AI model stopped responding: {exc}"
                ) from exc
        finally:
            # Closing the socket also tells llama-server to stop generating
            # if the browser went away mid-answer.
            connection.close()


def _describe_http_error(response: http.client.HTTPResponse) -> str:
    raw = response.read(2000).decode("utf-8", errors="replace")
    detail = raw.strip()

    try:
        error = json.loads(raw).get("error")
        if isinstance(error, dict):
            detail = error.get("message", detail)
        elif isinstance(error, str):
            detail = error
    except (ValueError, AttributeError):
        pass

    return f"The local AI model returned an error ({response.status}): {detail}"


def _parse_sse(response: http.client.HTTPResponse) -> Iterator[str]:
    """Yield text deltas from an OpenAI-style ``text/event-stream`` body."""
    for raw_line in iter(response.readline, b""):
        line = raw_line.decode("utf-8", errors="replace").strip()

        if not line.startswith("data:"):
            continue

        payload = line[len("data:"):].strip()

        if payload == "[DONE]":
            return

        try:
            event = json.loads(payload)
        except ValueError:
            continue  # ignore keep-alives or partial junk

        if isinstance(event, dict) and "error" in event:
            error = event["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise AIProviderError(f"The local AI model reported an error: {message}")

        choices = event.get("choices") or []

        if not choices:
            continue

        delta = (choices[0].get("delta") or {}).get("content")

        if delta:
            yield delta
