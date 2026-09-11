"""AP07/AP08：流式工具参数、usage 末块与断流恢复（P04,P06）。"""

from __future__ import annotations

import json

import pytest

from .fixture_server import (
    PlannedResponse,
    RouteScript,
    openai_completion,
    openai_stream_chunks,
    tool_call_delta,
)

CHAT_PATH = "/v1/chat/completions"


def _arguments_fragments(payload: str, size: int = 5) -> list[str]:
    return [payload[index:index + size] for index in range(0, len(payload), size)]


@pytest.mark.asyncio
async def test_ap07_fragmented_tool_arguments_are_delivered_exactly_once(model_harness) -> None:
    arguments = json.dumps({"file_path": "src/app.py", "limit": 25}, ensure_ascii=False)
    fragments = _arguments_fragments(arguments)
    tool_chunks = [tool_call_delta(index=0, call_id="call_1", name="Read", arguments="")]
    tool_chunks += [tool_call_delta(index=0, arguments=piece) for piece in fragments]

    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            sse_chunks=openai_stream_chunks(
                content="Need tool",
                tool_call_chunks=tool_chunks,
                finish_reason="tool_calls",
                prompt_tokens=12,
                completion_tokens=7,
            )
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    events = []
    async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "use tools"}],
        retry_enabled=False,
    ):
        events.append(event)

    tool_call_events = [event for event in events if event["type"] == "tool_call"]
    assert len(tool_call_events) == 1
    payload = tool_call_events[0]["tool_call"]
    assert payload["name"] == "Read"
    assert json.loads(payload["arguments"]) == {"file_path": "src/app.py", "limit": 25}

    done_events = [event for event in events if event["type"] == "done"]
    assert len(done_events) == 1
    assert done_events[0]["finish_reason"] == "tool_calls"
    assert [item["name"] for item in done_events[0]["tool_calls"]] == ["Read"]
    # usage 只来自末块，不按 chunk 累加
    assert done_events[0]["usage"]["input_tokens"] == 12
    assert done_events[0]["usage"]["output_tokens"] == 7


@pytest.mark.asyncio
async def test_ap07_multiple_tool_indexes_and_early_finish_with_usage_only_chunk(model_harness) -> None:
    tool_chunks = [
        tool_call_delta(index=0, call_id="call_a", name="Read", arguments='{"path":'),
        tool_call_delta(index=1, call_id="call_b", name="Grep", arguments='{"pattern":'),
        tool_call_delta(index=0, arguments='"a.py"}'),
        tool_call_delta(index=1, arguments='"needle"}'),
    ]
    chunks = openai_stream_chunks(
        content="",
        tool_call_chunks=tool_chunks,
        finish_reason="tool_calls",
        empty_chunks=2,
        prompt_tokens=30,
        completion_tokens=9,
    )
    # 把 usage-only 块放在 finish_reason 之前，模拟网关顺序差异
    usage_chunk = chunks[-2]
    finish_chunk = chunks[-1]
    ordered = chunks[:-2] + [usage_chunk, finish_chunk]
    model_harness.server.set_default(CHAT_PATH, PlannedResponse(sse_chunks=ordered))
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    events = []
    async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "use tools"}], retry_enabled=False
    ):
        events.append(event)

    calls = {event["tool_call"]["name"]: json.loads(event["tool_call"]["arguments"]) for event in events if event["type"] == "tool_call"}
    assert calls == {"Read": {"path": "a.py"}, "Grep": {"pattern": "needle"}}
    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    assert done[0]["usage"]["input_tokens"] == 30
    assert done[0]["usage"]["output_tokens"] == 9


@pytest.mark.asyncio
async def test_ap07_finish_reason_earlier_than_usage_only_chunk(model_harness) -> None:
    """网关把 usage-only 块放在 finish 之后时，仍只发一次 done 且带 usage。"""

    base = openai_stream_chunks(
        content="answer",
        finish_reason="stop",
        prompt_tokens=5,
        completion_tokens=3,
        include_usage_chunk=False,
    )
    usage_only = {
        "id": "chatcmpl-fixture",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fixture-model",
        "choices": [],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    model_harness.server.set_default(CHAT_PATH, PlannedResponse(sse_chunks=[*base, usage_only]))
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}], retry_enabled=False
    )]

    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    assert done[0]["content"] == "answer"
    assert done[0]["usage"]["input_tokens"] == 5


@pytest.mark.asyncio
async def test_ap08_half_tool_json_then_aborted_stream_is_not_executed(model_harness) -> None:
    """输出半个工具 JSON 后网络中断：不交付工具、不产生成功 done、partial 可见。"""

    tool_chunks = [
        tool_call_delta(index=0, call_id="call_1", name="Read", arguments='{"file_path": "src/'),
    ]
    chunks = openai_stream_chunks(content="", tool_call_chunks=tool_chunks, include_usage_chunk=False)
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(sse_chunks=chunks, streaming=True, truncate_after_chunks=2),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat", timeout=10)

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "use tools"}], retry_enabled=False
    )]

    assert [event["type"] for event in events if event["type"] == "tool_call"] == []
    assert [event["type"] for event in events if event["type"] == "done"] == []
    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["partial"] is True
    assert errors[0]["dropped_incomplete_tool_calls"] == ["Read"]
    assert model_harness.http_count() == 1

    model_harness.write_json(
        "ap08/half_tool_json.json",
        {
            "requirement": "P04,P06",
            "acceptance": "AP08",
            "http_requests": model_harness.http_count(),
            "events": [event["type"] for event in events],
            "dropped_incomplete_tool_calls": errors[0]["dropped_incomplete_tool_calls"],
        },
    )


@pytest.mark.asyncio
async def test_ap08_error_after_partial_output_reports_partial_and_known_usage(model_harness) -> None:
    """已输出部分响应后失败：记录 partial 与已到手 usage，不透明重放。"""

    first = openai_stream_chunks(content="partial answer", include_usage_chunk=False)
    model_harness.server.route(
        CHAT_PATH,
        RouteScript(
            by_index={
                0: PlannedResponse(sse_chunks=first, streaming=True, truncate_after_chunks=2),
                1: PlannedResponse(status=500, payload={"error": {"message": "upstream exploded"}}),
            }
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat", timeout=10)

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}], retry_enabled=True
    )]

    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["partial"] is True
    assert errors[0]["accumulated"].startswith("partial")
    assert [event["type"] for event in events if event["type"] == "done"] == []
    # 已交付部分输出后不允许 SDK 层透明重试
    assert model_harness.http_count() == 1


@pytest.mark.asyncio
async def test_ap07_non_stream_tool_calls_keep_ids_and_arguments(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            payload=openai_completion(
                content="",
                finish_reason="tool_calls",
                tool_calls=[{"id": "call_9", "name": "Grep", "arguments": '{"pattern": "x"}'}],
            )
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    result = await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")

    assert result["finish_reason"] == "tool_calls"
    assert result["tool_calls"][0]["id"] == "call_9"
    assert json.loads(result["tool_calls"][0]["arguments"]) == {"pattern": "x"}
