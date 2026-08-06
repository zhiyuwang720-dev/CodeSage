"""Token estimation and compaction threshold tests (phase 08 S2)."""

from codesage.ai import ContentBlock, Usage
from codesage.core import assistant_message, user_message
from codesage.engine.tokens import (
    DEFAULT_RESERVE_TOKENS,
    estimate_context_tokens,
    estimate_message_tokens,
    estimate_tokens,
    should_compact,
)


# ---- estimate_tokens ----

def test_plain_text_chars_per_4_ceil():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2  # ceil(5/4)
    assert estimate_tokens("a" * 100) == 25


def test_dense_json_uses_half_ratio():
    """Dense JSON costs ~2x tokens for the same char count (chars/2 not /4)."""
    json_text = '{"key":"value","list":[1,2,3],"nested":{"a":true}}'
    assert estimate_tokens(json_text) > estimate_tokens("x" * len(json_text))
    assert estimate_tokens(json_text) == 25  # ceil(50/2)


def test_message_string_content():
    assert estimate_message_tokens(user_message("a" * 40)) == 10


def test_message_block_contents():
    m = assistant_message(
        [
            ContentBlock(type="text", text="a" * 8),
            ContentBlock(type="thinking", text="b" * 4),
            ContentBlock(type="tool_use", id="t1", name="Bash", input={"command": "ls"}),
            ContentBlock(type="tool_result", tool_use_id="t1", content="c" * 4),
        ]
    )
    # text 2 + thinking 1 + tool_use(name+json) + tool_result 1 — all >= 1
    assert estimate_message_tokens(m) >= 2 + 1 + 1 + 1


# ---- estimate_context_tokens: usage anchor ----

def test_no_usage_estimates_everything():
    est = estimate_context_tokens([user_message("a" * 40), user_message("b" * 40)])
    assert est.last_usage_index is None
    assert est.usage_tokens == 0
    assert est.tokens == est.trailing_tokens == 20


def test_usage_anchor_counts_server_tokens_plus_trailing():
    history = [
        user_message("question"),
        assistant_message("answer", usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15)),
        user_message("a" * 40),  # 10 trailing tokens
    ]
    est = estimate_context_tokens(history)
    assert est.last_usage_index == 1
    assert est.usage_tokens == 15
    assert est.trailing_tokens == 10
    assert est.tokens == 25


def test_anchor_skips_error_and_zerousage_messages():
    history = [
        user_message("q"),
        assistant_message("real", usage=Usage(input_tokens=5, output_tokens=1, total_tokens=6)),
        assistant_message("boom", is_error=True, usage=Usage(total_tokens=99)),
        user_message("after"),
    ]
    est = estimate_context_tokens(history)
    assert est.last_usage_index == 1
    assert est.usage_tokens == 6
    assert est.trailing_tokens == estimate_message_tokens(history[2]) + estimate_message_tokens(history[3])


def test_anchor_uses_last_valid_assistant():
    history = [
        assistant_message("a1", usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2)),
        user_message("mid"),
        assistant_message("a2", usage=Usage(input_tokens=3, output_tokens=3, total_tokens=6)),
        user_message("tail"),
    ]
    est = estimate_context_tokens(history)
    assert est.last_usage_index == 2
    assert est.usage_tokens == 6


def test_context_tokens_from_usage_falls_back_to_input_plus_output():
    est = estimate_context_tokens(
        [
            assistant_message(
                "answer",
                usage=Usage(input_tokens=4, output_tokens=6, total_tokens=0),
            ),
            user_message("tail"),
        ]
    )
    assert est.usage_tokens == 10


# ---- should_compact ----

def test_should_compact_threshold():
    window = 128_000
    assert not should_compact(window - DEFAULT_RESERVE_TOKENS, window)
    assert should_compact(window - DEFAULT_RESERVE_TOKENS + 1, window)


def test_should_compact_custom_reserve():
    window = 10_000
    assert should_compact(8_001, window, reserve=2_000)
    assert not should_compact(8_000, window, reserve=2_000)
