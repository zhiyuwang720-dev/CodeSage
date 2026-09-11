"""AP06：并发请求的配置/认证/观测隔离（P02,P08）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.execution_plane.models.config import LLMConfig, LLMProvider, resolve_sdk_model

from .fixture_server import (
    PlannedResponse,
    anthropic_message,
    openai_completion,
    openai_stream_chunks,
)

OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_PATH = "/v1/messages"


@pytest.mark.asyncio
async def test_ap06_two_endpoints_do_not_cross_talk(model_harness) -> None:
    model_harness.server.set_default(
        OPENAI_PATH,
        PlannedResponse(payload=openai_completion(content="openai-side", model="model-a")),
    )
    model_harness.server.set_default(
        ANTHROPIC_PATH,
        PlannedResponse(payload=anthropic_message(content="anthropic-side", model="model-b")),
    )

    openai_service = model_harness.service(
        provider="deepseek",
        model="model-alpha",
        api_key="fixture-key-alpha",
        protocol="openai_chat",
    )
    anthropic_service = model_harness.service(
        provider="claude",
        model="claude-opus-4-8",
        api_key="fixture-key-beta",
        protocol="anthropic_messages",
        base_url=model_harness.server.root_url,
    )

    openai_result, anthropic_result = await asyncio.gather(
        openai_service.chat_completion(
            messages=[{"role": "user", "content": "alpha"}], purpose="connection_test"
        ),
        anthropic_service.chat_completion(
            messages=[{"role": "user", "content": "beta"}], purpose="agent_model_test"
        ),
    )

    openai_records = model_harness.server.requests(OPENAI_PATH)
    anthropic_records = model_harness.server.requests(ANTHROPIC_PATH)
    assert len(openai_records) == 1
    assert len(anthropic_records) == 1

    # 每个端点只收到自己的模型与凭据
    assert openai_records[0].body["model"] == "model-alpha"
    assert openai_records[0].headers.get("authorization") == "Bearer fixture-key-alpha"
    assert anthropic_records[0].body["model"] == "claude-opus-4-8"
    assert anthropic_records[0].headers.get("x-api-key") == "fixture-key-beta"

    assert openai_result["purpose"] == "connection_test"
    assert openai_result["provider"] == "deepseek"
    assert openai_result["model_snapshot"]["sdk_model"] == "openai/model-alpha"
    assert anthropic_result["purpose"] == "agent_model_test"
    assert anthropic_result["provider"] == "claude"
    assert anthropic_result["model_snapshot"]["transport"] == "anthropic_messages"

    model_harness.runtime.force_flush(2000)
    from app.infrastructure.observability.litellm_integration import get_recorder

    records = [record for record in get_recorder().records() if record.event == "success"]
    for record in records:
        assert record.model in {"model-alpha", "claude-opus-4-8"}
        assert record.purpose in {"connection_test", "agent_model_test"}

    model_harness.write_json(
        "ap06/concurrency_isolation.json",
        {
            "requirement": "P02,P08",
            "acceptance": "AP06",
            "openai_request_model": openai_records[0].body["model"],
            "anthropic_request_model": anthropic_records[0].body["model"],
            "callback_records": [
                {"provider": record.provider, "model": record.model, "call_id": record.call_id}
                for record in records
            ],
            "gap": "LiteLLM 1.98 并发 dispatch 下可能丢失部分 success callback（顺序执行时每次调用都有回调）",
        },
    )


@pytest.mark.asyncio
async def test_ap06_capture_disabled_leaves_no_content_in_spans(model_harness) -> None:
    model_harness.server.set_default(
        OPENAI_PATH,
        PlannedResponse(
            payload=openai_completion(content="TOP-SECRET-COMPLETION", model="fixture-model")
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    await service.chat_completion(
        messages=[{"role": "user", "content": "TOP-SECRET-PROMPT"}], purpose="review"
    )
    model_harness.runtime.force_flush(2000)
    model_harness.write_json(
        "ap06/capture_off.json",
        {
            "requirement": "P08",
            "acceptance": "AP06",
            "spans": [
                {
                    "name": span.name,
                    "attributes": {key: str(value) for key, value in dict(span.attributes).items()},
                }
                for span in model_harness.spans()
            ],
        },
    )
    for span in model_harness.spans():
        payload = json.dumps({key: str(value) for key, value in dict(span.attributes).items()})
        assert "TOP-SECRET" not in payload
        for event in span.events:
            assert "TOP-SECRET" not in json.dumps({key: str(value) for key, value in dict(event.attributes or {}).items()})


def test_ap06_sdk_model_mapping_is_pure() -> None:
    sdk_model, transport = resolve_sdk_model(LLMProvider.QWEN, "qwen3.7-max", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert sdk_model == "openai/qwen3.7-max"
    assert transport == "openai_compatible"

    prefixed, prefixed_transport = resolve_sdk_model(LLMProvider.CLAUDE, "anthropic/claude-opus-4-8", None)
    assert prefixed == "anthropic/claude-opus-4-8"
    assert prefixed_transport == "anthropic_messages"

    config = LLMConfig(provider=LLMProvider.OPENAI, api_key="secret-value", model="m", sdk_model="openai/m")
    assert "secret-value" not in json.dumps(config.snapshot(), ensure_ascii=False)
    assert "secret-value" not in repr(config)
