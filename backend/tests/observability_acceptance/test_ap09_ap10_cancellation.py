"""AP09/AP10：辅助调用迁移、取消与 deadline（P02,P04,P05,P10）。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.execution_plane.models.config import ModelCatalog, ModelConfigurationError
from app.execution_plane.models.service import LLMService

from .fixture_server import PlannedResponse, openai_completion, openai_stream_chunks

CHAT_PATH = "/v1/chat/completions"


@pytest.mark.asyncio
async def test_ap09_auxiliary_calls_use_the_single_sdk_exit(model_harness) -> None:
    """连接测试 / 前端配置测试 / prompts / audit sessions 辅助调用都走同一出口。"""

    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content='{"issues": []}', model="fixture-model"))
    )
    connection_service = model_harness.service(provider="deepseek", model="deepseek-chat")
    agent_service = model_harness.service(provider="deepseek", model="deepseek-chat")

    await connection_service.chat_completion(
        messages=[{"role": "user", "content": "ping"}], purpose="connection_test"
    )
    await agent_service.chat_completion(
        messages=[{"role": "user", "content": "agent test"}], purpose="agent_model_test"
    )

    records = model_harness.server.requests(CHAT_PATH)
    assert len(records) == 2

    # 代码分析辅助调用（prompts 端点使用）也必须走同一路径
    analysis_service = model_harness.service(provider="deepseek", model="deepseek-chat")
    analysis = await analysis_service.analyze_code(code="print(1)", language="python")
    assert analysis["issues"] == []
    assert len(model_harness.server.requests(CHAT_PATH)) == 3

    model_harness.write_json(
        "ap09/auxiliary_callers.json",
        {
            "requirement": "P02,P10",
            "acceptance": "AP09",
            "http_requests": len(model_harness.server.requests(CHAT_PATH)),
            "purposes": ["connection_test", "agent_model_test", "review(analyze_code)"],
        },
    )


@pytest.mark.asyncio
async def test_ap09_configuration_error_fails_before_sending(model_harness, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "LLM_API_KEY", "", raising=False)
    missing_key_service = LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": "deepseek",
                "llmModel": "deepseek-chat",
                "llmBaseUrl": model_harness.server.base_url,
                "endpointProtocol": "openai_chat",
            }
        }
    )
    with pytest.raises(ModelConfigurationError):
        await missing_key_service.chat_completion(messages=[{"role": "user", "content": "hi"}])
    assert model_harness.http_count() == 0


def test_ap09_catalog_only_advertises_supported_protocols() -> None:
    payload = {}
    for provider in ModelCatalog.supported_providers():
        metadata = ModelCatalog.provider_metadata(provider)
        payload[provider.value] = {
            "default_endpoint_protocol": metadata.get("default_endpoint_protocol"),
            "supported_endpoint_protocols": list(metadata.get("supported_endpoint_protocols") or []),
        }
    unsupported = {"openai_responses", "responses", "native"}
    for provider, metadata in payload.items():
        assert not (unsupported & set(metadata["supported_endpoint_protocols"])), provider
        assert metadata["default_endpoint_protocol"] not in unsupported, provider
    assert payload["baidu"]["supported_endpoint_protocols"] == []


def test_ap09_unavailable_protocol_raises_before_send() -> None:
    service = LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": "openai",
                "llmModel": "gpt-5.5",
                "llmApiKey": "k",
                "endpointProtocol": "openai_responses",
            }
        }
    )
    with pytest.raises(ModelConfigurationError):
        service.get_agent_config()


@pytest.mark.asyncio
async def test_ap10_cancelling_a_hung_stream_stops_without_new_requests(model_harness) -> None:
    hang = PlannedResponse(
        sse_chunks=openai_stream_chunks(content="never fully delivered"),
        streaming=True,
        delay_between_chunks_seconds=5.0,
    )
    model_harness.server.set_default(CHAT_PATH, hang)
    service = model_harness.service(provider="deepseek", model="deepseek-chat", timeout=30)

    events = []

    async def consume() -> None:
        async for event in service.chat_completion_stream(
            messages=[{"role": "user", "content": "hang"}], retry_enabled=False
        ):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.5)
    assert model_harness.http_count() == 1
    cancelled_at = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - cancelled_at

    await asyncio.sleep(1.0)
    assert elapsed < 10.0
    assert model_harness.http_count() == 1
    assert [event["type"] for event in events if event["type"] == "done"] == []

    model_harness.write_json(
        "ap10/cancel.json",
        {
            "requirement": "P04,P05",
            "acceptance": "AP10",
            "cancel_latency_seconds": round(elapsed, 3),
            "http_requests_after_cancel": model_harness.http_count(),
        },
    )


@pytest.mark.asyncio
async def test_ap10_first_token_timeout_is_enforced_and_releases_admission(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            sse_chunks=openai_stream_chunks(content="late"),
            streaming=True,
            delay_before_seconds=3.0,
        ),
    )
    service = LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": "deepseek",
                "llmModel": "deepseek-chat",
                "llmApiKey": "fixture-key",
                "llmBaseUrl": model_harness.server.base_url,
                "endpointProtocol": "openai_chat",
                "llmFirstTokenTimeout": 1,
                "llmStreamTimeout": 1,
            },
            "otherConfig": {"llmConcurrency": 1, "llmGapMs": 0},
        }
    )

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "slow"}], retry_enabled=False
    )]
    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["error_type"] == "stream_timeout"
    assert model_harness.http_count() == 1

    # 准入许可必须被释放：后续请求仍可执行
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="second", model="fixture-model"))
    )
    follow_up = await service.chat_completion(messages=[{"role": "user", "content": "after"}], purpose="review")
    assert follow_up["content"] == "second"


@pytest.mark.asyncio
async def test_ap10_deadline_does_not_reset_on_retry(model_harness) -> None:
    from .fixture_server import RouteScript

    model_harness.server.route(
        CHAT_PATH,
        RouteScript(
            by_index={
                0: PlannedResponse(
                    status=429,
                    payload={"error": {"message": "rate limit"}},
                    headers={"retry-after": "0"},
                ),
                1: PlannedResponse(
                    status=429,
                    payload={"error": {"message": "rate limit"}},
                    headers={"retry-after": "0"},
                ),
                2: PlannedResponse(payload=openai_completion(content="third", model="fixture-model")),
            }
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    async with asyncio.timeout(5.0):
        result = await service.chat_completion(messages=[{"role": "user", "content": "retry"}], purpose="review")

    assert result["content"] == "third"
    assert model_harness.http_count() == 3
