"""A02/T02: one business model.attempt owns the SDK litellm_request span."""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace

from app.infrastructure.observability.tracing import (
    bind_observability_context,
    get_tracer,
    reset_observability_context,
    span_attributes,
)
from tests.observability_acceptance.fixture_server import PlannedResponse, openai_completion

CHAT_PATH = "/v1/chat/completions"


def _spans_named(model_harness, name: str):
    return [span for span in model_harness.spans() if (span.name or "") == name]


@pytest.mark.asyncio
async def test_t02_business_attempt_is_the_only_model_attempt(model_harness) -> None:
    """SDK 层必须复用业务级 model.attempt，而不是再建一层。"""

    model_harness.server.set_default(
        CHAT_PATH,
        PlannedResponse(payload=openai_completion(content="ok", model="fixture-model")),
    )
    service = model_harness.service(provider="deepseek", model="deepseek-chat")

    attempt_id = "business-attempt-1"
    token = bind_observability_context(
        task_id="task-1",
        review_run_id="run-1",
        session_id="session-1",
        turn_id="turn-1",
        model_attempt_id=attempt_id,
        perspective="security",
        purpose="review",
    )
    attempt_span = get_tracer().start_span(
        "model.attempt",
        attributes={
            "openinference.span.kind": "CHAIN",
            **span_attributes(model_attempt_id=attempt_id, attempt_number=1),
        },
    )
    context_token = otel_context.attach(trace.set_span_in_context(attempt_span))
    try:
        result = await service.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            purpose="review",
            retry_owner="litellm_sdk",
        )
    finally:
        otel_context.detach(context_token)
        attempt_span.end()
        reset_observability_context(token)

    await asyncio.sleep(0.3)
    model_harness.runtime.force_flush(3000)

    assert result["content"] == "ok"
    attempts = _spans_named(model_harness, "model.attempt")
    llm_spans = _spans_named(model_harness, "litellm_request")
    admissions = _spans_named(model_harness, "model.admission")

    assert [s.name for s in model_harness.spans()].count("model.attempt") == 1, "SDK 层重复创建了 model.attempt"
    assert len(attempts) == 1
    assert len(llm_spans) == 1
    assert llm_spans[0].parent is not None
    assert llm_spans[0].parent.span_id == attempt_span.context.span_id, "litellm_request 未挂到业务 attempt"
    assert dict(attempts[0].attributes).get("codesage.model_attempt_id") == attempt_id
    assert dict(attempts[0].attributes).get("codesage.retry_owner") == "litellm_sdk"
    attempt_pid = dict(attempts[0].attributes).get("codesage.provider_request_id")
    llm_pid = dict(llm_spans[0].attributes).get("codesage.provider_request_id")
    assert attempt_pid, "业务 model.attempt 缺少 provider_request_id"
    assert attempt_pid == llm_pid, "业务 attempt 与 SDK span 的 provider_request_id 不一致"
    assert result["provider_request_id"] == attempt_pid, "LLMResponse 与 span 的 provider_request_id 不一致"
    assert admissions, "admission span 缺失"
    assert all(
        s.parent is not None and s.parent.span_id == attempt_span.context.span_id for s in admissions
    ), "model.admission 未挂到业务 attempt"
    for admission in admissions:
        attrs = dict(admission.attributes)
        assert attrs.get("codesage.model_attempt_id") == attempt_id, "admission 缺少 model_attempt_id"
        assert attrs.get("codesage.task_id") == "task-1", "admission 缺少 task_id"
        assert attrs.get("codesage.turn_id") == "turn-1", "admission 缺少 turn_id"
        assert attrs.get("codesage.perspective") == "security", "admission 缺少 perspective"
    assert not any(
        "gen_ai.request.model" in dict(s.attributes) and dict(s.attributes)["gen_ai.request.model"] == "review:security"
        for s in model_harness.spans()
    ), "视角别名污染了模型名字段"

    model_harness.write_json(
        "a02/trace_hierarchy.json",
        {
            "requirement": "T02",
            "acceptance": "A02",
            "model_attempt_spans": len(attempts),
            "litellm_request_spans": len(llm_spans),
            "model_admission_spans": len(admissions),
            "llm_parent_is_business_attempt": True,
            "admission_parent_is_business_attempt": True,
        },
    )