"""AP03：SDK 回调粒度与 HTTP 记录一致性（P03,P08）。"""

from __future__ import annotations

import asyncio

import pytest

from app.infrastructure.observability import litellm_integration as li

from .fixture_server import PlannedResponse, openai_completion, openai_stream_chunks

CHAT_PATH = "/v1/chat/completions"


async def _settle_callbacks(seconds: float = 0.3) -> None:
    """litellm 的成功回调可能作为 background task 在返回后落地。"""

    await asyncio.sleep(seconds)


class _CallbackCapture:
    """直接挂到 SDK 的 callback 桶上，记录真实回调调用（证据用）。"""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, kwargs, response_obj, start_time, end_time) -> None:
        payload = dict(kwargs or {})
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        litellm_params = payload.get("litellm_params") if isinstance(payload.get("litellm_params"), dict) else {}
        nested_metadata = litellm_params.get("metadata") if isinstance(litellm_params.get("metadata"), dict) else {}
        usage = payload.get("usage")
        if usage is None:
            dumped = getattr(response_obj, "model_dump", None)
            dumped = dumped() if callable(dumped) else None
            usage = (dumped or {}).get("usage") if isinstance(dumped, dict) else None
        self.calls.append(
            {
                "event": "success",
                "call_id": payload.get("litellm_call_id"),
                "model": payload.get("model"),
                "purpose": metadata.get("codesage_purpose") or nested_metadata.get("codesage_purpose"),
                "usage": dict(usage) if isinstance(usage, dict) else None,
                "response_cost": (payload.get("hidden_params") or {}).get("response_cost")
                if isinstance(payload.get("hidden_params"), dict)
                else None,
            }
        )


@pytest.fixture()
def callback_capture(model_harness):
    import litellm

    capture = _CallbackCapture()
    buckets = [litellm.callbacks, litellm.success_callback, litellm._async_success_callback]
    for bucket in buckets:
        bucket.append(capture)
    original_record = li.LiteLLMCallbackLogger._record

    def traced(self, event, kwargs, response_obj):
        print("TRACE _record", event, flush=True)
        return original_record(self, event, kwargs, response_obj)

    li.LiteLLMCallbackLogger._record = traced
    print(
        "TRACE buckets",
        [type(item).__name__ for item in litellm.callbacks],
        [type(item).__name__ for item in litellm._async_success_callback],
        [type(item).__name__ for item in litellm._async_failure_callback],
        flush=True,
    )
    try:
        yield capture
    finally:
        li.LiteLLMCallbackLogger._record = original_record
        for bucket in buckets:
            while capture in bucket:
                bucket.remove(capture)


@pytest.mark.asyncio
async def test_ap03_one_logical_call_records_one_http_request_and_one_callback(model_harness, callback_capture) -> None:
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="ok", model="fixture-model"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    li.get_recorder().clear()

    await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")

    await _settle_callbacks()

    http_records = model_harness.server.requests(CHAT_PATH)
    local_records = [record for record in li.get_recorder().records() if record.event == "success"]
    assert len(http_records) == 1
    assert len(callback_capture.calls) == 1, callback_capture.calls
    assert len(local_records) == 1, local_records
    assert callback_capture.calls[0]["purpose"] == "review"
    assert local_records[0].prompt_tokens == 11
    assert local_records[0].completion_tokens == 7

    model_harness.write_json(
        "ap03/callback_granularity.json",
        {
            "requirement": "P03,P08",
            "acceptance": "AP03",
            "http_requests": len(http_records),
            "sdk_callbacks": callback_capture.calls,
            "local_records": [
                {
                    "event": record.event,
                    "model": record.model,
                    "purpose": record.purpose,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "response_cost_usd": record.response_cost_usd,
                }
                for record in li.get_recorder().records()
            ],
            "note": "callback 粒度=每次 litellm SDK 调用一次；与服务器收到的实际请求数一致",
        },
    )


@pytest.mark.asyncio
async def test_ap03_streaming_call_does_not_emit_extra_logical_callbacks(model_harness, callback_capture) -> None:
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(sse_chunks=openai_stream_chunks(content="streamed"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    async for _ in service.chat_completion_stream(
        messages=[{"role": "user", "content": "hi"}], retry_enabled=False
    ):
        pass
    await _settle_callbacks()

    http_records = model_harness.server.requests(CHAT_PATH)
    assert len(http_records) == 1
    assert len(callback_capture.calls) == 1
    assert callback_capture.calls[0]["usage"]["prompt_tokens"] == 11


@pytest.mark.asyncio
async def test_ap03_repeated_callbacks_do_not_double_count_tokens(model_harness) -> None:
    """同一逻辑调用的重复 callback 不能被计两次。"""

    from app.infrastructure.observability.litellm_integration import LiteLLMCallbackLogger, PriceTable

    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="ok", model="fixture-model"))
    )
    recorder = li.get_recorder()
    recorder.clear()
    logger_instance = LiteLLMCallbackLogger(recorder, PriceTable())

    payload = {
        "litellm_call_id": "fixed-call",
        "model": "fixture-model",
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    await logger_instance.async_log_success_event(payload, None, None, None)
    await logger_instance.async_log_success_event(payload, None, None, None)
    counted = [record for record in recorder.records() if record.call_id == "fixed-call"]
    assert len(counted) == 1


@pytest.mark.asyncio
async def test_ap03_model_span_is_emitted_once_per_sdk_call(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH, PlannedResponse(payload=openai_completion(content="ok", model="fixture-model"))
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    await service.chat_completion(messages=[{"role": "user", "content": "hi"}], purpose="review")
    await _settle_callbacks()
    model_harness.runtime.force_flush(2000)
    spans = model_harness.spans()
    model_spans = [span for span in spans if (span.name or "").startswith("litellm_request")]
    assert len(model_spans) == 1, [span.name for span in spans]
    attributes = dict(model_spans[0].attributes)
    assert attributes.get("gen_ai.request.model")
    model_harness.write_json(
        "ap03/model_span.json",
        {
            "span_name": model_spans[0].name,
            "attributes": {key: str(value) for key, value in attributes.items()},
            "span_count": len(model_spans),
        },
    )
