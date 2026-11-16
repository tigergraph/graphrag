"""Token-based STM compaction helpers."""

from __future__ import annotations

try:
    import tiktoken
except ImportError:  # pragma: no cover - production requirements include it
    tiktoken = None


def token_count(text: str) -> int:
    if not text:
        return 0
    try:
        if tiktoken is None:
            raise RuntimeError("tokenizer unavailable")
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def message_text(messages) -> str:
    return "\n".join(
        f"{item.role}: {item.content}"
        if hasattr(item, "role")
        else f"{item.get('role')}: {item.get('content')}"
        for item in messages
    )


KEEP_NEWEST_TURNS = 2


def select_compaction_range(summary: str, messages, *, budget_tokens: int):
    """Summarize oldest turns once STM exceeds the agent budget."""
    total = token_count(summary) + token_count(message_text(messages))
    if total <= budget_tokens:
        return []
    keep_count = KEEP_NEWEST_TURNS * 2
    eligible = messages[:-keep_count] if len(messages) > keep_count else []
    selected = []
    remaining = total
    for item in eligible:
        selected.append(item)
        rendered = (
            f"{item.role}: {item.content}"
            if hasattr(item, "role")
            else f"{item.get('role')}: {item.get('content')}"
        )
        remaining -= token_count(rendered)
        if remaining <= budget_tokens:
            break
    return selected
