"""AP13/AP14：SDK 观测单 span、重复回调去重与成本容差（P08,P09）。"""

from __future__ import annotations

import pytest

from app.infrastructure.observability.litellm_integration import (
    COST_COMPARISON_TOLERANCE,
    COST_STATUS_PARTIAL,
    COST_STATUS_SDK_VERIFIED,
    COST_STATUS_UNKNOWN,
    COST_STATUS_UNPRICED,
    LiteLLMCallbackLogger,
    PriceEntry,
    PriceTable,
    configure_prices,
    get_price_table,
    get_recorder,
    install_litellm_integration,
)

from .fixture_server import PlannedResponse, RouteScript, openai_completion

CHAT_PATH = "/v1/chat/completions"
TEST_PRICE = PriceEntry(
    provider="deepseek",
    model="deepseek-chat",
    input_per_1k_usd="0.001",
    output_per_1k_usd="0.002",
    source="test-price",
)


async def _settle(seconds: float = 0.3) -> None:
    import asyncio

    await asyncio.sleep(seconds)


@pytest.mark.asyncio
async def test_ap13_one_provider_span_and_one_callback_per_call(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="ok", model="fixture-model"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    recorder = get_recorder()
    recorder.clear()

    await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    await _settle()
    model_harness.runtime.force_flush(2000)

    spans = [span for span in model_harness.spans() if (span.name or "").startswith("litellm_request")]
    records = [record for record in recorder.records() if record.event == "success"]
    assert len(spans) == 1
    assert len(records) == 1
    assert len(model_harness.server.requests(CHAT_PATH)) == 1

    attributes = dict(spans[0].attributes)
    assert attributes["openinference.span.kind"] == "LLM"
    assert attributes.get("codesage.provider_request_id")
    # 同一个逻辑调用只能有一个 LLM 级模型 span（不能有外包的第二份计费 span）
    llm_spans = [
        span
        for span in model_harness.spans()
        if dict(span.attributes).get("openinference.span.kind") == "LLM"
    ]
    assert len(llm_spans) == 1, [span.name for span in llm_spans]

    model_harness.write_json(
        "ap13/provider_span.json",
        {
            "requirement": "P08",
            "acceptance": "AP13",
            "span_names": [span.name for span in spans],
            "span_kind": attributes["openinference.span.kind"],
            "provider_request_id": attributes.get("codesage.provider_request_id"),
            "callback_call_ids": [record.call_id for record in records],
            "http_requests": len(model_harness.server.requests(CHAT_PATH)),
        },
    )


@pytest.mark.asyncio
async def test_ap13_repeated_callbacks_do_not_double_count(model_harness) -> None:
    recorder = get_recorder()
    recorder.clear()
    logger_instance = LiteLLMCallbackLogger(recorder)
    payload = {
        "litellm_call_id": "call-fixed",
        "model": "deepseek-chat",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    for _ in range(3):
        await logger_instance.async_log_success_event(payload, None, None, None)
        logger_instance.log_success_event(payload, None, None, None)
    records = [record for record in recorder.records() if record.call_id == "call-fixed"]
    assert len(records) == 1


@pytest.mark.asyncio
async def test_ap13_install_is_idempotent(model_harness) -> None:
    import litellm
    from litellm.integrations.opentelemetry import OpenTelemetry

    first = install_litellm_integration(capture_content=False)
    second = install_litellm_integration(capture_content=False)
    assert first["installed"] is True or first.get("_installed") is True
    assert second.get("_installed") is True
    otel_handlers = [item for item in litellm.callbacks if isinstance(item, OpenTelemetry)]
    assert len(otel_handlers) == 1


@pytest.mark.asyncio
async def test_ap13_successful_and_failed_calls_are_distinguishable(model_harness) -> None:
    model_harness.server.route(
        CHAT_PATH,
        RouteScript(
            by_index={
                0: PlannedResponse(payload=openai_completion(content="ok", model="fixture-model")),
                1: PlannedResponse(status=500, payload={"error": {"message": "boom"}}),
            }
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    recorder = get_recorder()
    recorder.clear()

    await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    await _settle()
    with pytest.raises(Exception):
        await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    await _settle()

    events = {record.event for record in recorder.records()}
    assert "success" in events
    assert "failure" in events


def test_ap14_cost_within_tolerance_and_unknown_not_zero() -> None:
    table = PriceTable([TEST_PRICE])
    verified = table.evaluate(
        model="deepseek-chat",
        usage={"prompt_tokens": 1000, "completion_tokens": 2000},
        response_cost=0.005,
    )
    assert verified.status == COST_STATUS_SDK_VERIFIED
    assert verified.value_usd == "0.005"
    assert verified.price_hash == table.price_hash

    tiny = table.evaluate(
        model="deepseek-chat",
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        response_cost=0.003,
    )
    assert tiny.status in {COST_STATUS_SDK_VERIFIED, COST_STATUS_PARTIAL}
    assert COST_COMPARISON_TOLERANCE < 1e-3

    unknown = table.evaluate(model="unknown-model", usage={"prompt_tokens": 10}, response_cost=None)
    assert unknown.status == COST_STATUS_UNKNOWN
    assert unknown.value_usd is None

    unpriced = table.evaluate(
        model="deepseek-chat",
        usage={"prompt_tokens": 1000, "completion_tokens": 0},
        response_cost=0.0,
    )
    assert unpriced.status == COST_STATUS_UNPRICED
    assert unpriced.reason == "sdk_default_zero_is_not_free"


def test_ap14_price_hash_changes_with_price_input() -> None:
    first = PriceTable([TEST_PRICE])
    second = PriceTable([PriceEntry(**{**TEST_PRICE.__dict__, "input_per_1k_usd": "0.002"})])
    assert first.price_hash != second.price_hash


@pytest.mark.asyncio
async def test_ap14_cost_snapshot_is_recorded(model_harness) -> None:
    configure_prices([TEST_PRICE])
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            payload=openai_completion(content="ok", model="deepseek-chat", prompt_tokens=1000, completion_tokens=2000)
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    recorder = get_recorder()
    recorder.clear()

    await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    await _settle()
    records = [record for record in recorder.records() if record.event == "success"]
    assert len(records) == 1
    cost = records[0].cost
    assert cost["price_hash"] == get_price_table().price_hash
    assert cost["status"] in {COST_STATUS_SDK_VERIFIED, COST_STATUS_PARTIAL, COST_STATUS_UNKNOWN}
    model_harness.write_json(
        "ap14/cost_snapshot.json",
        {
            "requirement": "P09",
            "acceptance": "AP14",
            "price_hash": cost["price_hash"],
            "sdk_raw_value": cost["sdk_raw_value"],
            "status": cost["status"],
            "tolerance": str(COST_COMPARISON_TOLERANCE),
        },
    )
    configure_prices([])
