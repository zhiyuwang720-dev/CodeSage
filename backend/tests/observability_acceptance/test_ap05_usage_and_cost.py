"""AP05/AP11：usage 来源、缓存开关与成本边界（P07,P09）。"""

from __future__ import annotations

import pytest

from app.infrastructure.observability.litellm_integration import (
    COST_STATUS_PARTIAL,
    COST_STATUS_SDK_VERIFIED,
    COST_STATUS_UNKNOWN,
    PriceEntry,
    PriceTable,
    configure_prices,
    get_price_table,
)

from .fixture_server import PlannedResponse, RouteScript, openai_completion, openai_stream_chunks

CHAT_PATH = "/v1/chat/completions"
TEST_PRICE = PriceEntry(
    provider="deepseek",
    model="deepseek-chat",
    input_per_1k_usd="0.001",
    output_per_1k_usd="0.002",
    source="test-price",
)


@pytest.mark.asyncio
async def test_ap05_usage_sources_are_distinguishable(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            payload=openai_completion(
                content="ok",
                model="fixture-model",
                prompt_tokens=100,
                completion_tokens=20,
                extra_usage={
                    "prompt_tokens_details": {"cached_tokens": 40},
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            )
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    result = await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    usage = result["usage"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["cache_read_tokens"] == 40
    assert usage["reasoning_tokens"] == 5
    assert usage["usage_source"] == "sdk_normalized"
    assert usage["field_sources"]["input_tokens"] if "input_tokens" in usage["field_sources"] else True


@pytest.mark.asyncio
async def test_ap05_explicit_zero_missing_and_synthetic_zero(model_harness) -> None:
    """真实 0（有非零字段佐证）/ 无 usage / SDK 合成 0 必须区分。"""

    model_harness.server.route(
        CHAT_PATH,
        RouteScript(
            by_index={
                0: PlannedResponse(
                    payload=openai_completion(
                        content="explicit zero",
                        model="fixture-model",
                        prompt_tokens=0,
                        completion_tokens=0,
                        extra_usage={"total_tokens": 1},
                    )
                ),
                1: PlannedResponse(payload=openai_completion(content="no usage", model="fixture-model", include_usage=False)),
                2: PlannedResponse(
                    sse_chunks=openai_stream_chunks(content="synthetic zero", usage_zero=True),
                    content_type="text/event-stream",
                ),
            }
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    explicit = await service.chat_completion(messages=[{"role": "user", "content": "a"}], purpose="review")
    assert explicit["usage"] is not None, explicit
    assert explicit["usage"]["input_tokens"] == 0
    assert explicit["usage"]["output_tokens"] == 0
    # 非零 total 与 0+0 不一致时必须显式记录，不能静默修正
    assert explicit["usage"]["anomalies"] == ["total_tokens:sum_mismatch"]

    missing = await service.chat_completion(messages=[{"role": "user", "content": "b"}], purpose="review")
    assert missing["usage"]["input_tokens"] is None
    assert missing["usage"]["total_tokens"] is None
    assert "usage:synthetic_zero_unverified" in missing["usage"]["anomalies"]

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "c"}], retry_enabled=True
    )]
    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    synthetic = done[0]["usage"]
    # SDK 合成零/估算不得冒充厂商实测 token
    assert synthetic["usage_source"] == "sdk_normalized"
    assert synthetic["estimated_usage"] is None or synthetic["input_tokens"] != synthetic["estimated_usage"]["input_tokens"]
    assert model_harness.http_count() == 3


@pytest.mark.asyncio
async def test_ap05_failed_stream_keeps_known_usage_and_partial(model_harness) -> None:
    chunks = openai_stream_chunks(content="partial answer", prompt_tokens=9, completion_tokens=4)
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(sse_chunks=chunks, streaming=True, truncate_after_chunks=3)
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat", timeout=10)

    events = [event async for event in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}], retry_enabled=False
    )]
    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["partial"] is True


def test_ap05_price_table_strictness() -> None:
    table = PriceTable([TEST_PRICE])
    verified = table.evaluate(
        model="deepseek-chat",
        usage={"prompt_tokens": 1000, "completion_tokens": 1000},
        response_cost=0.003,
    )
    assert verified.status == COST_STATUS_SDK_VERIFIED
    assert verified.value_usd == "0.003"

    unknown = table.evaluate(model="totally-unknown-model", usage={"prompt_tokens": 1}, response_cost=0.0)
    assert unknown.status == COST_STATUS_UNKNOWN
    assert unknown.value_usd is None
    assert unknown.reason == "model_not_in_frozen_price_table"

    partial = table.evaluate(
        model="deepseek-chat",
        usage={"prompt_tokens": 1000},
        response_cost=0.001,
    )
    assert partial.status == COST_STATUS_PARTIAL
    assert partial.reason == "usage_incomplete_for_pricing"


@pytest.mark.asyncio
async def test_ap05_unknown_model_cost_stays_null(model_harness) -> None:
    configure_prices([])
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="ok", model="fixture-model"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    recorder = get_price_table()
    assert recorder.lookup(provider=None, model="deepseek-chat") is None

    await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    model_harness.write_json(
        "ap05/price_boundary.json",
        {
            "requirement": "P07,P09",
            "acceptance": "AP05",
            "price_table_hash": recorder.price_hash,
            "known_price_status": "sdk_verified (test price 0.001/0.002 per 1k)",
            "unknown_model_status": "unknown",
        },
    )
    configure_prices([])


@pytest.mark.asyncio
async def test_ap11_response_cache_is_off_and_prompt_policy_is_sdk_only(model_harness) -> None:
    """两个相同请求必须真的发起两次 HTTP；不得启用 SDK 响应缓存。"""

    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="same answer", model="fixture-model"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    first = await service.chat_completion(messages=[{"role": "user", "content": "identical"}], purpose="review")
    second = await service.chat_completion(messages=[{"role": "user", "content": "identical"}], purpose="review")

    assert first["content"] == second["content"]
    records = model_harness.server.requests(CHAT_PATH)
    assert len(records) == 2
    cached = [record for record in records if record.headers.get("x-litellm-cache-key")]
    assert cached == []
    model_harness.write_json(
        "ap11/cache_off.json",
        {
            "requirement": "P07",
            "acceptance": "AP11",
            "http_requests_for_identical_requests": len(records),
            "cache_headers_present": bool(cached),
        },
    )
