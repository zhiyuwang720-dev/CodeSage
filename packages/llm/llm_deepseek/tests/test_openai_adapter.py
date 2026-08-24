"""OpenAI-compatible adapter conversion tests (fully offline)."""

import sys
from pathlib import Path

_FAMILY = Path(__file__).resolve().parents[2]  # 家族根 packages/llm
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根(CodeSage)
sys.path.insert(0, str(_FAMILY))
sys.path.insert(0, str(_ROOT / "cordis-py"))

import json

import httpx
import pytest

from llm import ContentBlock, LLMClient, LLMError, LLMRequest, Message, ModelProfile, ToolSpec
from llm_deepseek import OpenAICompatibleAdapter

BASE = "https://test.local/v1"


def _adapter(handler) -> OpenAICompatibleAdapter:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return OpenAICompatibleAdapter(ModelProfile(model="deepseek-chat", base_url=BASE), http)


# ---- request conversion ----

def test_message_conversion_tool_result_becomes_tool_role():
    """A user message carrying tool_result blocks becomes role=tool messages."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    adapter = OpenAICompatibleAdapter(ModelProfile(model="m", base_url=BASE), httpx.AsyncClient(transport=transport))

    request = LLMRequest(
        system="sys",
        messages=[
            Message(
                role="user",
                content=[
                    ContentBlock(type="tool_result", tool_use_id="tu1", content="result text"),
                ],
            )
        ],
    )
    payload = adapter._build_payload(request, stream=False)
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert payload["messages"][1] == {"role": "tool", "tool_call_id": "tu1", "content": "result text"}


def test_message_conversion_merged_user_tool_result_plus_text():
    """A merged user message [tool_result, text] keeps the text as a user message."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    adapter = OpenAICompatibleAdapter(ModelProfile(model="m", base_url=BASE), httpx.AsyncClient(transport=transport))

    request = LLMRequest(
        messages=[
            Message(
                role="user",
                content=[
                    ContentBlock(type="tool_result", tool_use_id="tu1", content="out"),
                    ContentBlock(type="text", text="then what?"),
                ],
            )
        ],
    )
    messages = adapter._build_payload(request, stream=False)["messages"]
    assert messages == [
        {"role": "tool", "tool_call_id": "tu1", "content": "out"},
        {"role": "user", "content": "then what?"},
    ]


def test_message_conversion_assistant_tool_use():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
    )
    adapter = OpenAICompatibleAdapter(ModelProfile(model="m", base_url=BASE), httpx.AsyncClient(transport=transport))
    request = LLMRequest(
        messages=[
            Message(
                role="assistant",
                content=[
                    ContentBlock(type="text", text="let me check"),
                    ContentBlock(type="tool_use", id="tu9", name="Read", input={"path": "/x"}),
                ],
            )
        ]
    )
    messages = adapter._build_payload(request, stream=False)["messages"]
    assert messages[0]["content"] == "let me check"
    assert messages[0]["tool_calls"][0]["function"]["name"] == "Read"
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "/x"}'


# ---- non-streaming response ----

async def test_non_streaming_full_response():
    def handler(req):
        body = json.loads(req.content)
        assert body["model"] == "deepseek-chat"
        assert body["tools"][0]["function"]["name"] == "Read"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "final answer",
                            "reasoning_content": "deep think",
                            "tool_calls": [
                                {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"cmd": "ls"}'}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "prompt_cache_hit_tokens": 6, "prompt_cache_miss_tokens": 4, "completion_tokens": 2, "total_tokens": 12},
                "model": "deepseek-chat",
            },
        )

    adapter = _adapter(handler)
    resp = await adapter.acomplete(
        LLMRequest(messages=[Message(role="user", content="hi")], tools=[ToolSpec(name="Read")])
    )
    assert resp.content[0] == ContentBlock(type="thinking", text="deep think")
    assert resp.content[1] == ContentBlock(type="text", text="final answer")
    tool = resp.content[2]
    assert tool.type == "tool_use" and tool.name == "Bash" and tool.input == {"cmd": "ls"}
    assert resp.stop_reason == "tool_use"
    assert resp.usage.input_tokens == 4  # cache miss counts as input
    assert resp.usage.cache_read_tokens == 6  # DeepSeek cache partition


async def test_http_error_raises_retryable():
    adapter = _adapter(lambda req: httpx.Response(429, headers={"retry-after": "3"}, text="slow"))

    with pytest.raises(LLMError) as exc_info:
        await adapter.acomplete(LLMRequest(messages=[Message(role="user", content="hi")]))
    assert exc_info.value.status_code == 429
    assert exc_info.value.retryable
    assert exc_info.value.retry_after_seconds == 3.0


# ---- streaming ----

def _stream_chunks():
    return [
        {"choices": [{"delta": {"reasoning_content": "think "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "Grep", "arguments": ""}}]},
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
        {"choices": [{"delta": {}, "finish_reason": None}]},  # trailing chunk
    ]


async def test_streaming_events():
    chunk_iter = iter(_stream_chunks())
    adapter = _adapter(lambda req: httpx.Response(200, text=_sse(chunk_iter)))

    events = [ev async for ev in adapter.astream(LLMRequest(messages=[], stream=True))]
    types = [e.type for e in events]
    assert types == [
        "thinking_delta",
        "text_delta",
        "text_delta",
        "tool_use_start",
        "tool_use_delta",
        "tool_use_delta",
        "done",
        "usage",
    ]
    assert events[3].tool_name == "Grep"
    assert events[4].input_json_delta == '{"q":'
    assert events[6].stop_reason == "tool_use"
    assert events[7].usage.input_tokens == 5


async def test_usage_on_chunk_with_choices():
    """DeepSeek may attach usage to a chunk that still carries choices."""
    chunks = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}},
    ]
    adapter = _adapter(lambda req: httpx.Response(200, text=_sse(iter(chunks))))
    events = [ev async for ev in adapter.astream(LLMRequest(messages=[], stream=True))]
    usage_events = [e for e in events if e.type == "usage"]
    assert len(usage_events) == 2  # captured both, caller takes the last
    assert usage_events[0].usage.input_tokens == 5


async def test_collect_assembles_tool_use():
    from llm import LLMClient

    chunk_iter = iter(_stream_chunks())
    adapter = _adapter(lambda req: httpx.Response(200, text=_sse(chunk_iter)))

    stream = adapter.astream(LLMRequest(messages=[], stream=True))
    resp = await LLMClient.collect(stream)
    assert resp.text == "hello world"
    tool = resp.content[-1]
    assert tool.type == "tool_use" and tool.input == {"q": "x"}
    assert resp.stop_reason == "tool_use"


def _sse(chunks):
    lines = [f"data: {json.dumps(c)}\n" for c in chunks]
    lines.append("data: [DONE]\n")
    return "".join(lines)


async def test_streaming_http_error_reads_body():
    """Streaming 4xx must yield an error event without ResponseNotRead
    (streaming responses need aread() before .text)."""
    adapter = _adapter(lambda req: httpx.Response(401, text="unauthorized"))
    events = [ev async for ev in adapter.astream(LLMRequest(messages=[Message(role="user", content="hi")], stream=True))]
    assert len(events) == 1
    assert events[0].type == "error"
    assert "401" in events[0].error
    assert "unauthorized" in events[0].error


async def test_streaming_http_error_body_read_failure():
    """Even if aread() fails, the error event must still be produced."""
    adapter = _adapter(lambda req: httpx.Response(500, text="boom"))

    async def broken_aread(self):
        raise httpx.ReadError("broken stream")

    original = httpx.Response.aread
    httpx.Response.aread = broken_aread  # type: ignore[method-assign]
    try:
        events = [ev async for ev in adapter.astream(LLMRequest(messages=[], stream=True))]
    finally:
        httpx.Response.aread = original  # type: ignore[method-assign]
    assert events[0].type == "error" and "500" in events[0].error
