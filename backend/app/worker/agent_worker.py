from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from arq.connections import RedisSettings
from arq.worker import Retry, func

from app.core.config import settings
from app.execution_plane.task_executor import execute_agent_task
from app.infrastructure.messaging.task_queue import AGENT_TASK_JOB_NAME
from app.infrastructure.observability import configure_observability, extract_trace_context, get_meter, get_tracer
from app.infrastructure.observability.tracing import (
    bind_evaluation_context,
    reset_evaluation_context,
    span_attributes,
)

logger = logging.getLogger(__name__)


def decode_task_payload(payload: Any) -> str:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    try:
        data = json.loads(str(payload))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid agent task payload") from exc
    task_id = str(data.get("task_id") or "").strip() if isinstance(data, dict) else ""
    if not task_id:
        raise ValueError("Agent task payload missing task_id")
    return task_id


async def run_worker() -> None:
    from arq.worker import Worker

    logging.basicConfig(level=logging.INFO)
    worker = Worker(
        WorkerSettings.functions,
        queue_name=WorkerSettings.queue_name,
        redis_settings=WorkerSettings.redis_settings,
        max_jobs=WorkerSettings.max_jobs,
        job_timeout=WorkerSettings.job_timeout,
        max_tries=WorkerSettings.max_tries,
        retry_jobs=WorkerSettings.retry_jobs,
        health_check_key=WorkerSettings.health_check_key,
    )
    await worker.async_run()


async def execute_agent_task_job(
    ctx: dict[str, Any],
    task_id: str,
    delivery_id: str | None = None,
    trace_carrier: dict[str, str] | None = None,
) -> str:
    evaluation_values: dict[str, object] = {"task_id": task_id}
    try:
        from app.db.session import async_session_factory
        from app.models.agent_task import AgentTask

        async with async_session_factory() as db:
            task = await db.get(AgentTask, task_id)
            review_scope = (((task.audit_scope or {}).get("pr_review") or {}) if task else {})
            evaluation_values.update(
                eval_run_id=review_scope.get("eval_run_id"),
                case_id=review_scope.get("case_id"),
            )
    except Exception:
        logger.warning("Unable to preload evaluation trace context for task %s", task_id, exc_info=True)
    evaluation_token = bind_evaluation_context(**evaluation_values)
    parent = extract_trace_context(trace_carrier)
    try:
        with get_tracer().start_as_current_span("execution.attempt", context=parent) as span:
            span.set_attributes(
                span_attributes(
                    task_id=task_id,
                    delivery_id=delivery_id,
                    propagation_missing=not bool(trace_carrier),
                )
            )
            logger.info("Agent worker picked task %s", task_id)
            executor = ctx.get("execute_agent_task", execute_agent_task)
            try:
                result = await executor(task_id, delivery_id=delivery_id)
                span.set_attribute("codesage.status", result)
                get_meter().create_counter("codesage.execution.attempt").add(1, {"status": str(result)})
                if result == "already_owned":
                    from app.control_plane.execution_ownership import LEASE_SECONDS

                    raise Retry(defer=LEASE_SECONDS + 1)
                return result
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("codesage.status", "failed")
                raise
    finally:
        reset_evaluation_context(evaluation_token)


async def startup_observability(ctx: dict[str, Any]) -> None:
    ctx["observability_runtime"] = configure_observability(
        service_name=f"{settings.OTEL_SERVICE_NAME}-worker",
        enabled=settings.OTEL_ENABLED,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        local_trace_path=settings.OTEL_LOCAL_TRACE_PATH,
        local_metric_path=settings.OTEL_LOCAL_METRIC_PATH,
        export_timeout_seconds=settings.OTEL_EXPORT_TIMEOUT_SECONDS,
        max_attribute_bytes=settings.OTEL_CAPTURE_MAX_BYTES,
    )


async def shutdown_observability(ctx: dict[str, Any]) -> None:
    runtime = ctx.get("observability_runtime")
    if runtime is not None:
        runtime.force_flush(5000)
        runtime.shutdown()


class WorkerSettings:
    functions = [func(execute_agent_task_job, name=AGENT_TASK_JOB_NAME)]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    queue_name = settings.AGENT_TASK_QUEUE_NAME
    max_jobs = settings.AGENT_WORKER_CONCURRENCY
    job_timeout = settings.AGENT_WORKER_JOB_TIMEOUT_SECONDS
    max_tries = settings.AGENT_WORKER_MAX_TRIES
    retry_jobs = True
    health_check_key = settings.AGENT_WORKER_HEALTH_CHECK_KEY
    on_startup = startup_observability
    on_shutdown = shutdown_observability


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
