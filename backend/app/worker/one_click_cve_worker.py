from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from arq.connections import RedisSettings
from arq.worker import func

from app.core.config import settings
from app.services.one_click_cve.runner import run_one_click_cve_batch
from app.services.one_click_cve.task_queue import ONE_CLICK_CVE_BATCH_JOB_NAME
from app.services.review_runtime.resume_job import run_audit_session_resume_job
from app.services.review_runtime.resume_queue import AUDIT_SESSION_RESUME_JOB_NAME

logger = logging.getLogger(__name__)


def decode_batch_payload(payload: Any) -> str:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    try:
        data = json.loads(str(payload))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid one-click CVE batch payload") from exc
    batch_id = str(data.get("batch_id") or "").strip() if isinstance(data, dict) else ""
    if not batch_id:
        raise ValueError("One-click CVE batch payload missing batch_id")
    return batch_id


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
    )
    await worker.async_run()


async def run_one_click_cve_batch_job(ctx: dict[str, Any], batch_id: str) -> None:
    logger.info("One-click CVE worker picked batch %s", batch_id)
    await run_one_click_cve_batch(batch_id)


async def resume_audit_session_job(ctx: dict[str, Any], session_id: str, resume_token: str) -> None:
    logger.info("Audit-session resume worker picked session %s", session_id)
    await run_audit_session_resume_job(session_id, resume_token)


class WorkerSettings:
    functions = [
        func(run_one_click_cve_batch_job, name=ONE_CLICK_CVE_BATCH_JOB_NAME),
        func(resume_audit_session_job, name=AUDIT_SESSION_RESUME_JOB_NAME),
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    queue_name = settings.ONE_CLICK_CVE_QUEUE_NAME
    max_jobs = settings.ONE_CLICK_CVE_WORKER_CONCURRENCY
    job_timeout = settings.ONE_CLICK_CVE_WORKER_JOB_TIMEOUT_SECONDS
    max_tries = settings.ONE_CLICK_CVE_WORKER_MAX_TRIES
    retry_jobs = True


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
