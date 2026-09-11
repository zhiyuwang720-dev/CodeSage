"""AP02：真实 SDK + 本地端点验证请求语义（P02/P04）。

证据：服务器收到的实际请求正文、SDK 请求模型、配置快照 hash。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from .fixture_server import (
    PlannedResponse,
    RouteScript,
    anthropic_message,
    anthropic_stream_lines,
    openai_completion,
    openai_stream_chunks,
)

CHAT_PATH = "/v1/chat/completions"


def _snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_ap02_openai_compatible_non_stream_keeps_configured_model_and_endpoint(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="ok", model="fixture-model"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat", protocol="openai_chat")

    result = await service.chat_completion(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
        parallel_tool_calls=True,
        purpose="review",
    )

    records = model_harness.server.requests(CHAT_PATH)
    assert len(records) == 1
    body = records[0].body
    # OpenAI 兼容族统一走 openai/ 前缀 + api_base；wire 上的模型名保持配置值。
    assert body["model"] == "deepseek-chat"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["tools"][0]["function"]["name"] == "Read"
    assert body["parallel_tool_calls"] is True
    assert body.get("stream") is not True

    assert result["configured_model"] == "deepseek-chat"
    assert result["request_model"] == "deepseek-chat"
    assert result["response_model"] == "fixture-model"
    assert result["provider"] == "deepseek"
    assert result["endpoint_id"] == f"http://127.0.0.1:{model_harness.server.port}"
    assert result["protocol"] == "openai_chat"
    snapshot = result["model_snapshot"]
    assert snapshot["sdk_model"] == "openai/deepseek-chat"
    assert snapshot["transport"] == "openai_compatible"
    assert "api_key" not in json.dumps(snapshot)

    model_harness.write_json(
        "ap02/openai_non_stream.json",
        {
            "requirement": "P02,P04",
            "acceptance": "AP02",
            "configured_model": result["configured_model"],
            "sdk_request_model": body["model"],
            "snapshot_hash": _snapshot_hash(snapshot),
            "server_body": body,
            "server_path": records[0].path,
        },
    )


@pytest.mark.asyncio
async def test_ap02_openai_compatible_stream_matches_non_stream_semantics(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            sse_chunks=openai_stream_chunks(content="streamed answer", model="fixture-model"),
            content_type="text/event-stream",
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat", protocol="openai_chat")

    events = []
    async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}],
        retry_enabled=False,
        purpose="review",
    ):
        events.append(event)

    records = model_harness.server.requests(CHAT_PATH)
    assert len(records) == 1
    body = records[0].body
    assert body["model"] == "deepseek-chat"
    assert body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "hi"}]

    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    assert done[0]["content"] == "streamed answer"
    assert done[0]["configured_model"] == "deepseek-chat"
    assert done[0]["request_model"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_ap02_anthropic_transport_maps_to_sdk_messages_api(model_harness) -> None:
    model_harness.server.set_default(
        "/v1/messages",
        PlannedResponse(
            payload=anthropic_message(content="claude says hi", model="claude-fixture"),
        ),
    )
    service = model_harness.service(
        provider="claude",
        model="claude-opus-4-8",
        protocol="anthropic_messages",
        base_url=model_harness.server.root_url,
    )

    result = await service.chat_completion(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        max_tokens=64,
    )

    records = model_harness.server.requests("/v1/messages")
    assert len(records) == 1
    body = records[0].body
    assert body["model"] == "claude-opus-4-8"
    assert body["system"] == [{"type": "text", "text": "sys"}]
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert body["max_tokens"] == 64

    assert result["configured_model"] == "claude-opus-4-8"
    assert result["request_model"] == "claude-opus-4-8"
    assert result["response_model"] == "claude-fixture"
    assert result["protocol"] == "anthropic_messages"
    assert result["content"] == "claude says hi"


@pytest.mark.asyncio
async def test_ap02_anthropic_transport_streams(model_harness) -> None:
    model_harness.server.set_default(
        "/v1/messages",
        PlannedResponse(
            sse_raw_lines=anthropic_stream_lines(content="claude stream", model="claude-fixture"),
            content_type="text/event-stream",
        ),
    )
    service = model_harness.service(
        provider="claude",
        model="claude-opus-4-8",
        protocol="anthropic_messages",
        base_url=model_harness.server.root_url,
    )

    events = []
    async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}],
        retry_enabled=False,
    ):
        events.append(event)

    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    assert done[0]["content"] == "claude stream"
    assert done[0]["request_model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_ap02_unknown_endpoint_protocol_fails_before_sending(model_harness) -> None:
    model_harness.server.set_default(CHAT_PATH, PlannedResponse(payload=openai_completion()))
    service = model_harness.service(provider="openai", model="fixture-model", protocol="native")

    from app.execution_plane.models.config import ModelConfigurationError

    with pytest.raises(ModelConfigurationError):
        await service.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert model_harness.server.requests(CHAT_PATH) == []
