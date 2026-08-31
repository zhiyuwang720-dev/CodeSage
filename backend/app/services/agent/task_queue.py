from __future__ import annotations

import inspect
from typing import Any, Optional

from app.core.config import settings

AGENT_TASK_JOB_NAME = "execute_agent_task"


def should_use_worker_queue() -> bool:
    return str(settings.AGENT_TASK_EXECUTION_MODE).strip().lower() == "worker"


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

    async def enqueue(self, task_id: str) -> None:
        pool = await self._pool()
        await pool.enqueue_job(
            AGENT_TASK_JOB_NAME,
            str(task_id),
            _job_id=f"agent-task:{task_id}",
            _queue_name=self.queue_name,
        )

    async def close(self) -> None:
        if not self._owns_pool or self.arq_pool is None:
            return
        close = getattr(self.arq_pool, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


async def enqueue_agent_task(task_id: str) -> None:
    queue = AgentTaskQueue()
    try:
        await queue.enqueue(task_id)
    finally:
        await queue.close()
