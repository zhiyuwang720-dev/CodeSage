"""Integration tests for LLMService — chat_completion and chat_completion_stream."""

import os
from typing import Any, Dict, List

import pytest

from app.service.llm.service import LLMService


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        pytest.skip("DEEPSEEK_API_KEY not set")
    return key


def _make_service(**overrides) -> LLMService:
    """Build an LLMService pointed at DeepSeek."""
    llm_config: Dict[str, Any] = {
        "llmProvider": "deepseek",
        "llmApiKey": _api_key(),
        "llmModel": "deepseek-v4-pro",
        "llmTimeout": 60000,
        "llmTemperature": 0.0,
        "llmMaxTokens": 512,
    }
    llm_config.update(overrides)
    return LLMService(user_config={"llmConfig": llm_config})


def _msgs(*pairs: str) -> List[Dict[str, str]]:
    """Build message list from alternating roles, e.g. _msgs("user","hi","assistant","hello")."""
    messages: List[Dict[str, str]] = []
    for i in range(0, len(pairs), 2):
        messages.append({"role": pairs[i], "content": pairs[i + 1]})
    return messages


# ---------------------------------------------------------------------------
# chat_completion
# ---------------------------------------------------------------------------

class TestChatCompletion:
    """Tests for LLMService.chat_completion()."""

    @pytest.mark.asyncio
    async def test_simple_message(self):
        """A single-user-message should return valid response dict."""
        svc = _make_service()
        result = await svc.chat_completion(
            messages=_msgs("user", "用一句话介绍 Python"),
        )
        assert isinstance(result, dict)
        assert result["content"], f"empty content: {result}"
        assert result["model"], f"empty model: {result}"
        assert result["usage"]["total_tokens"] > 0
        assert result["finish_reason"] is not None

    @pytest.mark.asyncio
    async def test_system_user_messages(self):
        """System + user messages should be handled correctly."""
        svc = _make_service()
        result = await svc.chat_completion(
            messages=_msgs(
                "system", "你是一个 JSON 专家，只输出 JSON。",
                "user", '输出 {"ok": true}，不要解释。',
            ),
        )
        assert result["content"], f"empty content: {result}"
        assert "{" in result["content"] and "}" in result["content"], (
            f"expected JSON-like output: {result['content'][:200]}"
        )

    @pytest.mark.asyncio
    async def test_temperature_0_deterministic(self):
        """temperature=0 should give identical outputs across calls."""
        svc = _make_service()
        msg = _msgs("user", "只回复 OK 两个字母，不要任何其他内容。")

        r1 = await svc.chat_completion(messages=msg, temperature=0.0)
        r2 = await svc.chat_completion(messages=msg, temperature=0.0)

        assert r1["content"].strip() == r2["content"].strip(), (
            f"temperature=0 should yield identical output:\n"
            f"  {r1['content']!r}\n  {r2['content']!r}"
        )

    @pytest.mark.asyncio
    async def test_usage_fields(self):
        """Response usage dict should contain all three token counts."""
        svc = _make_service()
        result = await svc.chat_completion(
            messages=_msgs("user", "hi"),
        )
        usage = result["usage"]
        assert usage["prompt_tokens"] > 0
        assert usage["completion_tokens"] > 0
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    @pytest.mark.asyncio
    async def test_override_max_tokens(self):
        """Explicit max_tokens should be respected."""
        svc = _make_service()
        result = await svc.chat_completion(
            messages=_msgs("user", "写一篇 50 字的小短文。"),
            max_tokens=30,
        )
        tokens = result["usage"]["completion_tokens"]
        print(f"completion_tokens={tokens} (max_tokens=30)")
        # Allow some headroom but definitely under the configured cap
        assert tokens <= 30, f"expected ≤30 completion tokens, got {tokens}"


# ---------------------------------------------------------------------------
# chat_completion_stream
# ---------------------------------------------------------------------------

class TestChatCompletionStream:
    """Tests for LLMService.chat_completion_stream()."""

    @pytest.mark.asyncio
    async def test_stream_emits_token_and_done(self):
        """Normal stream should yield token events and a final done event."""
        svc = _make_service()
        events: List[Dict[str, Any]] = []

        async for event in svc.chat_completion_stream(
            messages=_msgs("user", "用一句话介绍 Python"),
        ):
            events.append(event)

        assert len(events) >= 2, f"expected ≥2 events, got {len(events)}"

        types = [e["type"] for e in events]
        assert "token" in types, f"no token event in: {types}"
        assert "done" in types, f"no done event in: {types}"

    @pytest.mark.asyncio
    async def test_done_event_has_full_payload(self):
        """The final `done` event must carry the complete result."""
        svc = _make_service()

        async for event in svc.chat_completion_stream(
            messages=_msgs("user", "hello world"),
        ):
            if event["type"] == "done":
                assert event["content"], "done event has empty content"
                assert event["usage"]["total_tokens"] > 0
                assert event.get("finish_reason"), "done event missing finish_reason"
                break
        else:
            pytest.fail("stream ended without a 'done' event")

    @pytest.mark.asyncio
    async def test_accumulated_grows_monotonically(self):
        """Each token event's `accumulated` should be a prefix of the final content."""
        svc = _make_service()
        accumulated_snapshots: List[str] = []

        async for event in svc.chat_completion_stream(
            messages=_msgs("user", "用中文回复：Python 是什么？"),
        ):
            if event["type"] == "token":
                accumulated_snapshots.append(event.get("accumulated", ""))

        assert len(accumulated_snapshots) > 0, "no token events received"
        # Verify monotonic growth
        for i in range(1, len(accumulated_snapshots)):
            prev = accumulated_snapshots[i - 1]
            cur = accumulated_snapshots[i]
            assert cur.startswith(prev), (
                f"accumulated doesn't grow monotonically at index {i}:\n"
                f"  prev={prev!r}\n  cur ={cur!r}"
            )

    @pytest.mark.asyncio
    async def test_stream_fallback_when_adapter_lacks_stream(self):
        """If the adapter has no stream_complete, chat_completion_stream
        should fall back to non-stream chat_completion and emit fake chunks."""
        svc = _make_service()
        events: List[Dict[str, Any]] = []
        types: List[str] = []

        async for event in svc.chat_completion_stream(
            messages=_msgs("user", "hi"),
        ):
            events.append(event)
            types.append(event["type"])

        # DeepSeekAdapter has no stream_complete, so this tests the fallback
        assert "done" in types, f"no done event, types={types}"
        done = events[-1]
        assert done["content"], "done event has empty content in fallback mode"
        assert done["usage"]["total_tokens"] > 0
