"""Anthropic adapter tests (offline, MockTransport)."""

import sys
from pathlib import Path

_FAMILY = Path(__file__).resolve().parents[2]  # 家族根 packages/llm
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根(CodeSage)
sys.path.insert(0, str(_FAMILY))
sys.path.insert(0, str(_ROOT / "cordis-py"))

import json

import httpx
import pytest

from llm import ContentBlock, LLMError, LLMRequest, Message, ModelProfile, ToolSpec
from llm_anthropic import AnthropicAdapter

BASE = "https://api.anthropic.com"


def _adapter(handler) -> tuple[AnthropicAdapter, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return AnthropicAdapter(ModelProfile(model="claude-sonnet", base_url=BASE), http), http


async def test_non_streaming_passthrough():
    def handler(req):
        assert req.headers["x-api-key"] == "sk-test"
        assert req.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(req.content)
        assert body["system"] == "sys"
        assert body["tools"][0]["name"] == "Read"
        assert body["messages"][0]["content"][0]["type"] == "tool_result"
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "answer"},
                    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"cmd": "ls"}},
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 7,
                    "cache_write_input_tokens": 3,
                },
                "model": "claude-sonnet",
            },
        )

    adapter, http = _adapter(handler)
    profile = adapter.profile
    profile.api_key_env = "ANTHROPIC_TEST_KEY"  # placeholder, key below is inline

    import os

    os.environ["ANTHROPIC_TEST_KEY"] = "sk-test"
    try:
        resp = await adapter.acomplete(
            LLMRequest(
                system="sys",
                messages=[
                    Message(
                        role="user",
                        content=[ContentBlock(type="tool_result", tool_use_id="tu1", content="out")],
                    )
                ],
                tools=[ToolSpec(name="Read")],
            )
        )
    finally:
        os.environ.pop("ANTHROPIC_TEST_KEY", None)
    assert resp.content[0].type == "thinking"
    assert resp.content[1].text == "answer"
    assert resp.content[2].name == "Bash"
    assert resp.usage.cache_read_tokens == 7
    assert resp.usage.cache_write_tokens == 3
    await http.aclose()


async def test_streaming_events():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 4, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu9", "name": "Grep"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q": "x"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    ]
    sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"

    adapter, http = _adapter(lambda req: httpx.Response(200, text=sse))
    got = [ev async for ev in adapter.astream(LLMRequest(messages=[], stream=True))]
    # message_start carries usage → usage event precedes the deltas
    assert [e.type for e in got] == [
        "usage",
        "text_delta",
        "text_delta",
        "tool_use_start",
        "tool_use_delta",
        "done",
    ]
    assert got[0].usage.input_tokens == 4
    assert got[3].tool_name == "Grep"
    assert got[4].input_json_delta == '{"q": "x"}'
    assert got[5].stop_reason == "tool_use"
    await http.aclose()


async def test_http_error_retryable():
    adapter, http = _adapter(lambda req: httpx.Response(503, text="unavailable"))
    with pytest.raises(LLMError) as exc_info:
        await adapter.acomplete(LLMRequest(messages=[Message(role="user", content="hi")]))
    assert exc_info.value.status_code == 503
    assert exc_info.value.retryable
    await http.aclose()
