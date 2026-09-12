"""Explicit, budgeted real-provider smoke for the fixed public case."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.infrastructure.observability import configure_observability
from app.infrastructure.observability.tracing import bind_observability_context, reset_observability_context
from app.execution_plane.models.service import LLMService

FIXED_CASE_ID = "deepseek-public-v1"
FIXED_PROMPT = "Return exactly CODESAGE_SMOKE_OK and nothing else."


@dataclass(frozen=True)
class SmokeResult:
    case_id: str
    review_run_id: str
    status: str
    provider: str
    configured_model: str
    request_model: str | None
    response_model: str | None
    content: str
    usage: dict[str, Any] | None
    estimated_cost: str | None
    max_cost: str
    currency: str
    reason: str | None = None


async def run_smoke(
    *,
    case_id: str,
    allow_paid: bool,
    max_cost: str,
    currency: str,
) -> SmokeResult:
    if not allow_paid:
        raise PermissionError("real smoke requires explicit --allow-paid")
    if case_id != FIXED_CASE_ID:
        raise ValueError(f"unsupported smoke case: {case_id}")
    try:
        budget = Decimal(max_cost)
    except InvalidOperation as exc:
        raise ValueError("max-cost must be a decimal") from exc
    if budget <= 0:
        raise ValueError("max-cost must be positive")
    if not settings.LLM_API_KEY or not settings.LLM_MODEL:
        raise RuntimeError("LLM configuration is incomplete; smoke not sent")

    run_id = f"smoke-{case_id}-{uuid4().hex}"
    observability = configure_observability(
        service_name=f"{settings.OTEL_SERVICE_NAME}-smoke",
        enabled=settings.OTEL_ENABLED,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        local_trace_path=settings.OTEL_LOCAL_TRACE_PATH,
        local_metric_path=settings.OTEL_LOCAL_METRIC_PATH,
        export_timeout_seconds=settings.OTEL_EXPORT_TIMEOUT_SECONDS,
        max_attribute_bytes=settings.OTEL_CAPTURE_MAX_BYTES,
        capture_content=settings.OTEL_CAPTURE_CONTENT,
    )
    correlation_token = bind_observability_context(
        task_id="diagnostics-smoke",
        review_run_id=run_id,
        session_id=run_id,
        turn_id="smoke-turn-1",
        model_attempt_id="smoke-attempt-1",
        perspective="smoke",
        purpose="smoke",
        case_id=case_id,
    )
    service = LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": settings.LLM_PROVIDER,
                "llmApiKey": settings.LLM_API_KEY,
                "llmModel": settings.LLM_MODEL,
                "llmBaseUrl": settings.LLM_BASE_URL or "",
            },
            "otherConfig": {"llmConcurrency": 1, "llmGapMs": 0},
        }
    )
    try:
        result = await service.chat_completion(
            messages=[{"role": "user", "content": FIXED_PROMPT}],
            max_tokens=32,
            temperature=0,
            purpose="smoke",
            retry_owner="sdk",
        )
    finally:
        await asyncio.sleep(0.5)
        observability.force_flush(5000)
        observability.shutdown()
        reset_observability_context(correlation_token)
    response_cost = result.get("response_cost_usd")
    estimated = None
    if response_cost is not None:
        estimated = str(Decimal(str(response_cost)))
        if estimated and Decimal(estimated) > budget:
            return SmokeResult(
                case_id=case_id,
                review_run_id=run_id,
                status="over_budget",
                provider=str(result.get("provider") or settings.LLM_PROVIDER),
                configured_model=str(result.get("configured_model") or settings.LLM_MODEL),
                request_model=result.get("request_model"),
                response_model=result.get("response_model"),
                content=str(result.get("content") or ""),
                usage=result.get("usage"),
                estimated_cost=estimated,
                max_cost=max_cost,
                currency=currency.upper(),
                reason="estimated_cost_exceeds_budget",
            )
    return SmokeResult(
        case_id=case_id,
        review_run_id=run_id,
        status="passed" if estimated is not None else "content_verified_price_unknown",
        provider=str(result.get("provider") or settings.LLM_PROVIDER),
        configured_model=str(result.get("configured_model") or settings.LLM_MODEL),
        request_model=result.get("request_model"),
        response_model=result.get("response_model"),
        content=str(result.get("content") or ""),
        usage=result.get("usage"),
        estimated_cost=estimated,
        max_cost=max_cost,
        currency=currency.upper(),
        reason=None if estimated is not None else "response cost unavailable",
    )


def smoke_payload(result: SmokeResult) -> dict[str, Any]:
    return asdict(result)


def run_smoke_sync(**kwargs: Any) -> SmokeResult:
    return asyncio.run(run_smoke(**kwargs))
