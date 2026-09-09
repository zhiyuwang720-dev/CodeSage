from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import httpx


def run_doctor(*, api_url: str, phoenix_url: str, timeout_seconds: float = 30) -> dict[str, object]:
    correlation_id = f"doctor-{uuid4()}"
    checks: dict[str, object] = {}
    with httpx.Client(timeout=5) as client:
        api = client.get(f"{api_url.rstrip('/')}/health")
        api.raise_for_status()
        checks["api"] = True
        phoenix = client.get(f"{phoenix_url.rstrip('/')}/healthz")
        phoenix.raise_for_status()
        checks["phoenix"] = True

    from redis import Redis
    redis_client = Redis.from_url(os.getenv("REDIS_URL", "redis://eval-redis:6379/0"))
    checks["redis"] = bool(redis_client.ping())
    checks["workers"] = {
        key: bool(redis_client.get(key))
        for key in ("codesage:eval:worker-1", "codesage:eval:worker-2")
    }

    import psycopg
    database_url = os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    with psycopg.connect(database_url, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select current_database()")
            checks["postgresql"] = cursor.fetchone()[0]

    local_path = Path("/var/lib/codesage/observability/eval-doctor.traces.otlp.jsonl")
    os.environ["OTEL_PROJECT_NAME"] = os.getenv("CODESAGE_PHOENIX_PROJECT", "codesage-eval")
    from app.infrastructure.observability import configure_observability, get_tracer
    from app.infrastructure.observability.tracing import span_attributes
    runtime = configure_observability(
        service_name="codesage-eval-doctor", enabled=True,
        endpoint=os.getenv("CODESAGE_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces"),
        local_trace_path=str(local_path),
    )
    with get_tracer().start_as_current_span("eval.doctor.probe") as span:
        span.set_attributes(span_attributes(eval_run_id=correlation_id, case_id="doctor", correlation_id=correlation_id))
        trace_id = f"{span.get_span_context().trace_id:032x}"
    runtime.force_flush(5000)
    runtime.shutdown()
    checks["local_otlp_jsonl"] = local_path.exists() and correlation_id in local_path.read_text(encoding="utf-8")

    from codesage_eval.phoenix_adapter import PhoenixAdapter
    spans = PhoenixAdapter(base_url=phoenix_url).wait_for_trace(
        project_identifier=os.getenv("CODESAGE_PHOENIX_PROJECT", "codesage-eval"),
        eval_run_id=correlation_id, case_id="doctor", timeout_seconds=timeout_seconds,
    )
    checks["trace"] = {"found": bool(spans), "trace_id": trace_id, "correlation_id": correlation_id}
    healthy = all([checks["api"], checks["phoenix"], checks["redis"], checks["local_otlp_jsonl"], bool(spans)])
    healthy = healthy and all(checks["workers"].values())
    return {"healthy": healthy, "checks": checks}
