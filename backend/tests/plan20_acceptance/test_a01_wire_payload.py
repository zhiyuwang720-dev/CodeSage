"""A01: final request payload and model identity are the observed facts.

This is the W00 wire-level half of A01.  Content-artifact equivalence is added
by the content-capture workstream; these assertions already prevent the
regression where a perspective alias becomes the billed request model.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.observability_acceptance.fixture_server import (
    PlannedResponse,
    openai_completion,
    openai_stream_chunks,
)

CHAT_PATH = "/v1/chat/completions"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


def _body_hash(body: dict) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.asyncio
async def test_a01_non_stream_payload_uses_wire_model_not_perspective_alias(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(payload=openai_completion(content="ok", model="fixture-model")),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    messages = [
        {"role": "system", "content": "system instruction"},
        {"role": "user", "content": "inspect this patch"},
    ]

    result = await service.chat_completion(
        messages=messages,
        tools=TOOLS,
        parallel_tool_calls=True,
        temperature=0.1,
        max_tokens=128,
        agent_type="review:security",
        purpose="review",
    )

    records = model_harness.server.requests(CHAT_PATH)
    assert len(records) == 1
    body = records[0].body
    assert body["model"] == "deepseek-chat"
    assert body["messages"] == messages
    assert body["tools"] == TOOLS
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 128
    assert body["parallel_tool_calls"] is True
    assert body.get("stream") is not True

    assert result["request_model"] == "deepseek-chat"
    assert result["perspective"] == "security"
    assert result["request_model"] != "review:security"
    assert result["provider_request_id"]

    model_harness.write_json(
        "a01/wire_non_stream.json",
        {
            "requirement": "D01,T02",
            "acceptance": "A01",
            "payload_fidelity": "wire_equivalent",
            "server_body_hash": _body_hash(body),
            "server_body": body,
            "request_model": result["request_model"],
            "perspective": result["perspective"],
            "provider_request_id": result["provider_request_id"],
        },
    )


@pytest.mark.asyncio
async def test_a01_stream_payload_matches_non_stream_semantics(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            sse_chunks=openai_stream_chunks(content="streamed", model="fixture-model"),
            content_type="text/event-stream",
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    messages = [
        {"role": "system", "content": "system instruction"},
        {"role": "user", "content": "inspect this patch"},
    ]

    events = []
    async for event in service.chat_completion_stream(
        messages=messages,
        tools=TOOLS,
        parallel_tool_calls=True,
        temperature=0.1,
        max_tokens=128,
        agent_type="review:security",
        retry_enabled=False,
        purpose="review",
    ):
        events.append(event)

    records = model_harness.server.requests(CHAT_PATH)
    assert len(records) == 1
    body = records[0].body
    assert body["model"] == "deepseek-chat"
    assert body["messages"] == messages
    assert body["tools"] == TOOLS
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 128
    assert body["parallel_tool_calls"] is True
    assert body["stream"] is True

    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    assert done[0]["request_model"] == "deepseek-chat"
    assert done[0]["perspective"] == "security"

    model_harness.write_json(
        "a01/wire_stream.json",
        {
            "requirement": "D01,T02",
            "acceptance": "A01",
            "payload_fidelity": "wire_equivalent",
            "server_body_hash": _body_hash(body),
            "server_body": body,
            "request_model": done[0]["request_model"],
            "perspective": done[0]["perspective"],
        },
    )

