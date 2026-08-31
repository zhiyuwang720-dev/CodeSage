from __future__ import annotations

import inspect
from typing import Any, Optional

from app.core.config import settings


AUDIT_SESSION_RESUME_JOB_NAME = "resume_audit_session"


class AuditSessionResumeQueue:
    def __init__(self, *, arq_pool: Optional[Any] = None):
        self.arq_pool = arq_pool
        self._owns_pool = arq_pool is None

    async def _pool(self):
        if self.arq_pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            self.arq_pool = await create_pool(
                RedisSettings.from_dsn(settings.REDIS_URL),
                default_queue_name=settings.ONE_CLICK_CVE_QUEUE_NAME,
            )
        return self.arq_pool

    async def enqueue(self, session_id: str, resume_token: str) -> None:
        pool = await self._pool()
        await pool.enqueue_job(
            AUDIT_SESSION_RESUME_JOB_NAME,
            str(session_id),
            str(resume_token),
            _job_id=f"audit-session-resume:{session_id}:{resume_token}",
            _queue_name=settings.ONE_CLICK_CVE_QUEUE_NAME,
        )

    async def close(self) -> None:
        if not self._owns_pool or self.arq_pool is None:
            return
        close = getattr(self.arq_pool, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


async def enqueue_audit_session_resume(session_id: str, resume_token: str) -> None:
    queue = AuditSessionResumeQueue()
    try:
        await queue.enqueue(session_id, resume_token)
    finally:
        await queue.close()
