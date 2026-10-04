from dataclasses import dataclass

# Room kept free for ORION's own system prompt and for some chat history, so
# attaching a file can never leave the model unable to see the conversation.
SYSTEM_RESERVE_TOKENS = 300
HISTORY_RESERVE_TOKENS = 600


def estimate_tokens(text: str) -> int:
    """Cheap, deliberately pessimistic token estimate (no tokenizer needed)."""
    return len(text) // 3 + 1


@dataclass(frozen=True)
class FileBudget:
    total_tokens: int  # how many tokens of attached files fit in one request
    ctx_size: int


def file_budget(ctx_size: int, max_tokens: int) -> FileBudget:
    reply_reserve = min(max_tokens, ctx_size // 2)
    available = (
        ctx_size - reply_reserve - SYSTEM_RESERVE_TOKENS - HISTORY_RESERVE_TOKENS
    )

    return FileBudget(total_tokens=max(0, available), ctx_size=ctx_size)
