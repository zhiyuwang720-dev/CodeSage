from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job
from redis.asyncio import Redis
from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent_task import AgentTask, AgentTaskStatus
from app.models.checkpoint import AuditStageORM
from app.models.project import Project
from app.models.review_execution import ReviewExecutionRun
from app.models.user import User


pytestmark = pytest.mark.skipif(
    os.getenv("CODESAGE_WORKER_ACCEPTANCE") != "1",
    reason="仅由一键验收启动真实依赖后运行",
)


def _worker(log_path: Path) -> tuple[subprocess.Popen, object]:
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "arq", "tests.worker_acceptance.acceptance_worker.WorkerSettings", "--burst"],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=os.environ.copy(),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return process, handle


async def _security_barrier(task_id: str) -> ReviewExecutionRun:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        async with async_session_factory() as db:
            stage = await db.scalar(select(AuditStageORM).where(
                AuditStageORM.task_id == task_id,
                AuditStageORM.stage_type == "review:security",
                AuditStageORM.status == "completed",
            ))
            row = await db.get(ReviewExecutionRun, task_id)
            if stage is not None and row is not None and row.attempt_id:
                return row
        await asyncio.sleep(0.1)
    raise TimeoutError("security stage barrier timed out")


async def _completion_barrier(redis: Redis, record_key: str) -> dict[str, str]:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        record = await redis.hgetall(record_key)
        if record.get("result") == AgentTaskStatus.COMPLETED:
            return record
        await asyncio.sleep(0.1)
    raise TimeoutError("takeover did not complete within 45 seconds")


@pytest.mark.asyncio
@pytest.mark.parametrize("repetition", range(3))
async def test_killed_owner_is_automatically_taken_over_after_lease_expiry(repetition):
    suffix = uuid4().hex
    task_id, user_id, project_id = str(uuid4()), str(uuid4()), str(uuid4())
    workspace = Path(settings.MANAGED_PROJECTS_ROOT) / ".auditai_workspaces" / "projects" / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    diff = workspace / "review.diff"
    diff.write_text(f"diff --git a/a.py b/a.py\n+crash-{repetition}\n", encoding="utf-8")
    async with async_session_factory() as db:
        db.add(User(id=user_id, email=f"crash-{suffix}@example.test", hashed_password="x"))
        db.add(Project(id=project_id, name=f"crash-{suffix}", owner_id=user_id))
        db.add(AgentTask(
            id=task_id, project_id=project_id, created_by=user_id,
            version_label="acceptance", task_type="pr_review",
            status=AgentTaskStatus.PENDING,
            audit_scope={"pr_review": {"diff_file_path": str(diff)}},
        ))
        await db.commit()

    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    pool = await create_pool(
        RedisSettings.from_dsn(os.environ["REDIS_URL"]),
        default_queue_name=os.environ["AGENT_TASK_QUEUE_NAME"],
    )
    control = f"acceptance:control:{task_id}"
    record = f"acceptance:{task_id}"
    calls = f"acceptance:model_calls:{task_id}"
    await redis.delete(control, record, calls)
    await redis.hset(control, mapping={"block_non_security": "1"})
    logs = Path(os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"])
    processes: list[subprocess.Popen] = []
    handles: list[object] = []
    try:
        await pool.enqueue_job(
            "acceptance_execute", task_id, "crash-owner",
            _job_id=f"crash-owner:{task_id}",
        )
        owner, owner_log = _worker(logs / f"crash-{repetition}-owner.log")
        processes.append(owner)
        handles.append(owner_log)
        original = await _security_barrier(task_id)
        original_run_id = (original.identity_json or {})["run_id"]
        original_attempt = original.attempt_id
        calls_before = await redis.hgetall(calls)

        await pool.enqueue_job(
            "acceptance_execute", task_id, "crash-takeover",
            _job_id=f"crash-takeover:{task_id}",
        )
        takeover, takeover_log = _worker(logs / f"crash-{repetition}-takeover.log")
        processes.append(takeover)
        handles.append(takeover_log)
        await asyncio.sleep(0.5)
        owner.kill()
        await asyncio.to_thread(owner.wait, 10)
        await redis.hset(control, mapping={"release": "1"})

        result = await _completion_barrier(redis, record)
        assert int(result["worker_id"].split(":", 1)[1]) == takeover.pid
        calls_after = await redis.hgetall(calls)
        assert calls_after["review:security"] == calls_before["review:security"]
        async with async_session_factory() as db:
            task = await db.get(AgentTask, task_id)
            row = await db.get(ReviewExecutionRun, task_id)
            assert task.status == AgentTaskStatus.COMPLETED
            assert (row.identity_json or {})["run_id"] == original_run_id
            assert row.lease_epoch > original.lease_epoch
            assert original_attempt is not None
            stages = (await db.execute(select(AuditStageORM).where(
                AuditStageORM.task_id == task_id
            ))).scalars().all()
            assert len(stages) == 5
            assert all(stage.status == "completed" for stage in stages)
    finally:
        for job_id in (f"crash-owner:{task_id}", f"crash-takeover:{task_id}"):
            try:
                await Job(
                    job_id, pool, _queue_name=os.environ["AGENT_TASK_QUEUE_NAME"]
                ).abort(timeout=1)
            except Exception:
                pass
        await pool.close()
        await redis.aclose()
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        for handle in handles:
            handle.close()
