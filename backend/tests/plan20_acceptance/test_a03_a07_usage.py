"""A03-A07: model identity, usage provenance, and protocol normalization."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.execution_plane.models.client import SDKModelClient, _StreamState
from app.execution_plane.models.config import LLMConfig, LLMRequest
from app.execution_plane.models.errors import ModelResponseError
from app.execution_plane.models.types import LLMProvider
from app.execution_plane.models.usage import normalize_usage
from app.execution_plane.runtime.bridge import RuntimeLLMModelClient
from tests.observability_acceptance.fixture_server import (
    PlannedResponse,
    anthropic_message,
    openai_completion,
    openai_stream_chunks,
)

CHAT_PATH = "/v1/chat/completions"


def _last_llm_span(model_harness):
    spans = [span for span in model_harness.spans() if (span.name or "").startswith("litellm_request")]
    assert spans, [span.name for span in model_harness.spans()]
    return spans[-1]


@pytest.mark.asyncio
async def test_a03_usage_survives_sdk_service_bridge_and_openinference(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            payload=openai_completion(
                content="done",
                model="deepseek-chat-202609",
                prompt_tokens=1000,
                completion_tokens=200,
                extra_usage={
                    "prompt_tokens_details": {"cached_tokens": 400},
                    "completion_tokens_details": {"reasoning_tokens": 50},
                },
            )
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    client = RuntimeLLMModelClient(llm_service=service, agent_type="review:security")

    response = await client.complete(
        system_prompt="review",
        recon_payload={},
        transcript=[],
        model_name="review:security",
        tool_definitions=[],
    )
    await asyncio.sleep(0.2)
    model_harness.runtime.force_flush(2000)

    assert response.request_model == "deepseek-chat"
    assert response.response_model == "deepseek-chat-202609"
    assert response.perspective == "security"
    assert response.usage is not None
    assert response.usage["input_tokens"] == 1000
    assert response.usage["output_tokens"] == 200
    assert response.usage["total_tokens"] == 1200
    assert response.usage["cache_read_tokens"] == 400
    assert response.usage["reasoning_tokens"] == 50
    assert response.usage["field_sources"]["total_tokens"] in {"sdk_normalized", "derived"}

    span = _last_llm_span(model_harness)
    attributes = dict(span.attributes)
    assert attributes.get("llm.token_count.prompt") == 1000, attributes
    assert attributes["llm.token_count.completion"] == 200
    assert attributes["llm.token_count.total"] == 1200
    assert attributes["llm.token_count.prompt_details.cache_read"] == 400
    assert attributes["llm.token_count.completion_details.reasoning"] == 50

    model_harness.write_json(
        "a03/usage_layers.json",
        {
            "requirement": "U01,U02,U03,T02",
            "acceptance": "A03",
            "request_model": response.request_model,
            "response_model": response.response_model,
            "usage": response.usage,
            "span_attributes": {
                key: attributes.get(key)
                for key in (
                    "llm.token_count.prompt",
                    "llm.token_count.completion",
                    "llm.token_count.total",
                    "llm.token_count.prompt_details.cache_read",
                    "llm.token_count.completion_details.reasoning",
                )
            },
        },
    )


def test_a04_missing_explicit_zero_and_synthetic_zero_are_distinct(acceptance_artifact_root) -> None:
    assert normalize_usage(None, provider="deepseek") is None

    explicit_zero = normalize_usage(
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        provider="deepseek",
        zero_fidelity="provider",
    )
    assert explicit_zero is not None
    assert explicit_zero.prompt_tokens == 0
    assert explicit_zero.field_sources["prompt_tokens"] == "sdk_normalized"

    synthetic_zero = normalize_usage(
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        provider="deepseek",
        zero_fidelity="unverified",
    )
    assert synthetic_zero is not None
    assert synthetic_zero.prompt_tokens is None
    assert synthetic_zero.total_tokens is None
    assert "usage:synthetic_zero_unverified" in synthetic_zero.anomalies

    target = acceptance_artifact_root / "evidence" / "a04" / "usage_fidelity.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "requirement": "U02",
                "acceptance": "A04",
                "missing": None,
                "explicit_zero": explicit_zero.to_dict(include_raw=True),
                "synthetic_zero": synthetic_zero.to_dict(include_raw=True),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_a04_wire_missing_usage_does_not_become_provider_zero(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(payload=openai_completion(content="no usage", include_usage=False)),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    result = await service.chat_completion(messages=[{"role": "user", "content": "hi"}])

    usage = result["usage"]
    assert usage is not None
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["total_tokens"] is None


def test_a05_estimated_usage_is_independent_from_provider_fields() -> None:
    usage = normalize_usage(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        provider="deepseek",
        estimated_input_tokens=140,
        estimated_output_tokens=30,
    )
    assert usage is not None
    assert usage.prompt_tokens == 100
    assert usage.total_tokens == 120
    assert usage.estimated_usage == {
        "input_tokens": 140,
        "output_tokens": 30,
        "total_tokens": 170,
        "method": "tiktoken",
        "version": "1",
    }


def test_a05_partial_error_event_preserves_known_usage(acceptance_artifact_root) -> None:
    state = _StreamState(
        content="partial",
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        raw_usage_seen=True,
        chunk_count=1,
    )
    config = LLMConfig(
        provider=LLMProvider.DEEPSEEK,
        api_key="fixture-key",
        model="deepseek-chat",
        base_url="http://127.0.0.1:9",
    )
    request = LLMRequest(messages=[{"role": "user", "content": "hi"}], purpose="review")
    event = SDKModelClient()._error_event(
        ModelResponseError("stream interrupted"),
        state=state,
        max_attempts=1,
        attempts_used=1,
        config=config,
        request=request,
        truncated=True,
    )

    assert event["partial"] is True
    assert event["stream_truncated"] is True
    assert event["usage"]["input_tokens"] == 100
    assert event["usage"]["output_tokens"] == 20
    target = acceptance_artifact_root / "evidence" / "a05" / "partial_usage.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected_anomaly"),
    [
        (
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 999},
            "total_tokens:sum_mismatch",
        ),
        (
            {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 120}},
            "cache_read_tokens:exceeds_prompt",
        ),
        (
            {"prompt_tokens": -1, "completion_tokens": 20},
            "prompt_tokens:negative",
        ),
    ],
)
def test_a06_anomalies_are_preserved(raw: dict, expected_anomaly: str, acceptance_artifact_root) -> None:
    usage = normalize_usage(raw, provider="deepseek")
    assert usage is not None
    assert expected_anomaly in usage.anomalies
    target = acceptance_artifact_root / "evidence" / "a06" / "anomalies.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    if target.exists():
        payload = json.loads(target.read_text(encoding="utf-8"))
    payload[expected_anomaly] = {"raw": raw, "normalized": usage.to_dict(include_raw=True)}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_a06_derived_total_requires_both_input_and_output() -> None:
    derived = normalize_usage({"prompt_tokens": 10, "completion_tokens": 2}, provider="deepseek")
    assert derived is not None
    assert derived.total_tokens == 12
    assert derived.field_sources["total_tokens"] == "derived"

    incomplete = normalize_usage({"prompt_tokens": 10}, provider="deepseek")
    assert incomplete is not None
    assert incomplete.total_tokens is None
    assert incomplete.field_sources["total_tokens"] == "missing"


@pytest.mark.asyncio
async def test_a07_openai_cached_details_and_deepseek_hit_miss(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(
            payload=openai_completion(
                content="ok",
                prompt_tokens=1000,
                completion_tokens=200,
                extra_usage={
                    "prompt_tokens_details": {"cached_tokens": 400},
                    "prompt_cache_hit_tokens": 400,
                    "prompt_cache_miss_tokens": 600,
                },
            )
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    result = await service.chat_completion(messages=[{"role": "user", "content": "hi"}])

    assert result["usage"]["input_tokens"] == 1000
    assert result["usage"]["cache_read_tokens"] == 400
    assert result["usage"]["total_tokens"] == 1200

    model_harness.write_json(
        "a07/openai_compatible_usage.json",
        {
            "requirement": "U02,C04",
            "acceptance": "A07",
            "provider": "deepseek",
            "usage": result["usage"],
        },
    )


@pytest.mark.asyncio
async def test_a07_anthropic_cache_is_not_added_twice(model_harness) -> None:
    model_harness.server.set_default(
        "/v1/messages",
        PlannedResponse(
            payload=anthropic_message(
                content="ok",
                model="claude-fixture",
                input_tokens=600,
                output_tokens=200,
                cache_read_input_tokens=300,
                cache_creation_input_tokens=100,
            )
        ),
    )
    service = model_harness.service(
        provider="claude",
        model="claude-opus-4-8",
        protocol="anthropic_messages",
        base_url=model_harness.server.root_url,
    )
    result = await service.chat_completion(messages=[{"role": "user", "content": "hi"}], max_tokens=64)

    assert result["usage"]["input_tokens"] == 1000
    assert result["usage"]["output_tokens"] == 200
    assert result["usage"]["total_tokens"] == 1200
    assert result["usage"]["cache_read_tokens"] == 300
    assert result["usage"]["cache_write_tokens"] == 100

    model_harness.write_json(
        "a07/anthropic_cache_usage.json",
        {
            "requirement": "U02,C04",
            "acceptance": "A07",
            "provider": "claude",
            "usage": result["usage"],
        },
    )
