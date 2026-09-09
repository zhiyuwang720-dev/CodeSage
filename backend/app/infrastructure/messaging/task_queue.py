from __future__ import annotations

import inspect
from typing import Any, Optional

from app.core.config import settings
from app.infrastructure.observability.tracing import get_meter, get_tracer, inject_trace_context, span_attributes

AGENT_TASK_JOB_NAME = "execute_agent_task"


def should_use_worker_queue() -> bool:
    mode = str(settings.AGENT_TASK_EXECUTION_MODE).strip().lower()
    if mode != "worker":
        raise RuntimeError(
            "AGENT_TASK_EXECUTION_MODE 仅支持 worker；请启动 Redis 与 CodeSage worker"
        )
    return True


class AgentTaskQueue:
    def __init__(
        self,
        *,
        arq_pool: Optional[Any] = None,
        redis_url: Optional[str] = None,
        queue_name: Optional[str] = None,
    ):
        self.arq_pool = arq_pool
        self.redis_url = redis_url or settings.REDIS_URL
        self.queue_name = queue_name or settings.AGENT_TASK_QUEUE_NAME
        self._owns_pool = arq_pool is None

    async def _pool(self):
        if self.arq_pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            self.arq_pool = await create_pool(
                RedisSettings.from_dsn(self.redis_url),
                default_queue_name=self.queue_name,
            )
        return self.arq_pool

    async def enqueue(self, task_id: str, *, delivery_id: str | None = None) -> None:
        tracer = get_tracer()
        with tracer.start_as_current_span("task.publish") as span:
            span.set_attributes(span_attributes(task_id=str(task_id), delivery_id=delivery_id))
            pool = await self._pool()
            job_id = f"agent-task:{task_id}"
            args: tuple[Any, ...] = (str(task_id),)
            if delivery_id:
                job_id = f"{job_id}:{delivery_id}"
                args = (str(task_id), str(delivery_id))
            carrier = inject_trace_context()
            if carrier:
                if not delivery_id:
                    args = (str(task_id), None)
                args = (*args, carrier)
            try:
                await pool.enqueue_job(
                    AGENT_TASK_JOB_NAME,
                    *args,
                    _job_id=job_id,
                    _queue_name=self.queue_name,
                )
                get_meter().create_counter("codesage.task.publish").add(1, {"status": "success"})
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("codesage.status", "failed")
                get_meter().create_counter("codesage.task.publish").add(1, {"status": "failed"})
                raise

    async def close(self) -> None:
        if not self._owns_pool or self.arq_pool is None:
            return
        close = getattr(self.arq_pool, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


async def enqueue_agent_task(task_id: str, *, delivery_id: str | None = None) -> None:
    queue = AgentTaskQueue()
    try:
        await queue.enqueue(task_id, delivery_id=delivery_id)
    finally:
        await queue.close()
