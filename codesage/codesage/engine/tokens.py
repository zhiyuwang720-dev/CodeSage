"""Token estimation and compaction threshold (phase 08, specs/08 §3.2).

Estimation never calls the API: the last assistant usage block (server-
exact) anchors the count, and only messages after it are estimated — the
same "usage anchor" strategy as pi's estimateContextTokens and Claude
Code's tokenCountWithEstimation (error typically <5%).
"""

from __future__ import annotations

from ..ai import ContentBlock, Usage
from ..core import SessionMessage

#: Rough chars-per-token (pi estimateTokens / CC tokenCountWithEstimation).
CHARS_PER_TOKEN = 4
#: Dense JSON (tool args/results) costs more tokens per char.
DENSE_JSON_CHARS_PER_TOKEN = 2
#: Dense-JSON heuristic: share of JSON punctuation chars in the text.
DENSE_JSON_PUNCT = frozenset('"{}[]:,')
DENSE_JSON_RATIO = 0.3  # ponytail: empirical threshold; CC uses a similar ratio

#: Fallback window when the model profile has no context_window (DeepSeek 128K).
DEFAULT_CONTEXT_WINDOW = 128_000
#: Tokens reserved for summary prompt + output (pi DEFAULT_COMPACTION_SETTINGS).
DEFAULT_RESERVE_TOKENS = 16_384


def estimate_tokens(text: str) -> int:
    """Estimate tokens for one text: chars/4; dense JSON gets chars/2."""
    if not text:
        return 0
    density = sum(1 for c in text if c in DENSE_JSON_PUNCT) / len(text)
    per_token = DENSE_JSON_CHARS_PER_TOKEN if density >= DENSE_JSON_RATIO else CHARS_PER_TOKEN
    return max(1, -(-len(text) // per_token))  # ceil division


def estimate_block_tokens(block: ContentBlock) -> int:
    # the internal contract has no image blocks (both adapters are text-only);
    # when images arrive, give them a fixed allowance like pi's 4800 chars
    text = getattr(block, "text", None)
    if isinstance(text, str) and text:
        return estimate_tokens(text)
    if block.type == "tool_use":
        import json as _json

        try:
            encoded = _json.dumps(block.input, ensure_ascii=False)
        except TypeError:
            encoded = str(block.input)
        return estimate_tokens(f"{block.name or ''} {encoded}")
    if block.type == "tool_result":
        return estimate_tokens(str(block.content or ""))
    return 0


def estimate_message_tokens(message: SessionMessage) -> int:
    content = message.content
    if isinstance(content, str):
        return estimate_tokens(content)
    return sum(estimate_block_tokens(b) for b in content)


def context_tokens_from_usage(usage: Usage | None) -> int:
    """Server-reported context tokens (cache counts included)."""
    if usage is None:
        return 0
    return usage.total_tokens or usage.input_tokens + usage.output_tokens


class ContextEstimate:
    """Estimated context usage with the usage anchor decomposed."""

    __slots__ = ("tokens", "usage_tokens", "trailing_tokens", "last_usage_index")

    def __init__(self, tokens: int, usage_tokens: int, trailing_tokens: int, last_usage_index: int | None):
        self.tokens = tokens
        self.usage_tokens = usage_tokens
        self.trailing_tokens = trailing_tokens
        self.last_usage_index = last_usage_index


def estimate_context_tokens(messages: list[SessionMessage]) -> ContextEstimate:
    """Anchor on the last assistant usage block; estimate only what follows."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.role == "assistant" and not msg.is_error and msg.usage:
            usage_tokens = context_tokens_from_usage(msg.usage)
            if usage_tokens > 0:
                trailing = sum(estimate_message_tokens(m) for m in messages[i + 1 :])
                return ContextEstimate(usage_tokens + trailing, usage_tokens, trailing, i)
    estimated = sum(estimate_message_tokens(m) for m in messages)
    return ContextEstimate(estimated, 0, estimated, None)


def should_compact(tokens: int, window: int, reserve: int = DEFAULT_RESERVE_TOKENS) -> bool:
    """Compaction fires when context tokens exceed window - reserve."""
    return tokens > window - reserve
