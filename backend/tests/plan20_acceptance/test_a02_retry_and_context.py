"""A02/T01/T02: request correlation, retry backoff, and provider request truth."""

from __future__ import annotations

import asyncio

import pytest

from app.infrastructure.observability.tracing import (
    bind_observability_context,
    reset_observability_context,
)
from tests.observability_acceptance.fixture_server import (
    PlannedResponse,
    RouteScript,
    openai_completion,
)

CHAT_PATH = "/v1/chat/completions"
RATE_LIMIT = PlannedResponse(
    status=429,
    payload={"error": {"message": "rate limit", "type": "rate_limit_error", "code": "rate_limit"}},
    headers={"retry-after": "0"},
)


def _spans_named(model_harness, name: str):
    return [span for span in model_harness.spans() if (span.name or "") == name]


@pytest.mark.asyncio
async def test_t01_correlation_context_reaches_sdk_span(model_harness) -> None:
    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(payload=openai_completion(content="ok", model="fixture-model")),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    token = bind_observability_context(
        task_id="task-1",
        review_run_id="run-1",
        delivery_id="delivery-1",
        execution_attempt_id="exec-attempt-1",
        lease_epoch=2,
        session_id="session-1",
        turn_id="turn-1",
        model_attempt_id="model-attempt-1",
        perspective="security",
        purpose="review",
    )
    try:
        await service.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            agent_type="review:security",
            retry_owner="harness",
        )
    finally:
        reset_observability_context(token)
    await asyncio.sleep(0.2)
    model_harness.runtime.force_flush(2000)

    span = _spans_named(model_harness, "litellm_request")[-1]
    attributes = dict(span.attributes)
    expected = {
        "session.id": "session-1",
        "codesage.task_id": "task-1",
        "codesage.review_run_id": "run-1",
        "codesage.delivery_id": "delivery-1",
        "codesage.execution_attempt_id": "exec-attempt-1",
        "codesage.lease_epoch": 2,
        "codesage.session_id": "session-1",
        "codesage.turn_id": "turn-1",
        "codesage.model_attempt_id": "model-attempt-1",
        "codesage.perspective": "security",
        "codesage.purpose": "review",
    }
    for key, value in expected.items():
        assert attributes.get(key) == value, (key, attributes)

    model_harness.write_json(
        "a02/context_attributes.json",
        {
            "requirement": "T01,T02",
            "acceptance": "A02",
            "provider_request_id": attributes.get("codesage.provider_request_id"),
            "attributes": {key: attributes.get(key) for key in expected},
        },
    )


@pytest.mark.asyncio
async def test_a02_retry_has_one_attempt_and_backoff_span_per_real_request(model_harness) -> None:
    model_harness.server.route(
        CHAT_PATH,
        RouteScript(
            by_index={
                0: RATE_LIMIT,
                1: RATE_LIMIT,
                2: PlannedResponse(payload=openai_completion(content="recovered", model="fixture-model")),
            }
        ),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")
    result = await service.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        purpose="connection_test",
    )
    await asyncio.sleep(0.2)
    model_harness.runtime.force_flush(2000)

    assert result["content"] == "recovered"
    assert model_harness.http_count() == 3
    attempt_spans = _spans_named(model_harness, "model.attempt")
    backoff_spans = _spans_named(model_harness, "retry.backoff")
    llm_spans = _spans_named(model_harness, "litellm_request")
    assert len(attempt_spans) == 3
    assert len(backoff_spans) == 2
    assert len(llm_spans) == 3

    attempt_ids = {dict(span.attributes).get("codesage.model_attempt_id") for span in attempt_spans}
    assert len(attempt_ids) == 3
    assert all(attempt_ids)
    assert all(
        dict(span.attributes).get("codesage.retry_layer") == "sdk"
        for span in backoff_spans
    )
    # retries are children of the attempt that failed, not detached root spans
    attempt_span_ids = {span.context.span_id for span in attempt_spans}
    assert all(span.parent is not None and span.parent.span_id in attempt_span_ids for span in backoff_spans)

    model_harness.write_json(
        "a02/retry_request_mapping.json",
        {
            "requirement": "T02,U01",
            "acceptance": "A02",
            "http_requests": model_harness.http_count(),
            "attempt_spans": len(attempt_spans),
            "backoff_spans": len(backoff_spans),
            "provider_spans": len(llm_spans),
            "attempt_ids": sorted(attempt_ids),
        },
    )
