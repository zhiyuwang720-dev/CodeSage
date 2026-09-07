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
from app.models.agent_task import AgentFinding, AgentTask, AgentTaskStatus
from app.models.checkpoint import AuditStageORM
from app.models.project import Project
from app.models.review_execution import ReviewExecutionRun
from app.models.user import User
from app.services.pr_review.lifecycle import task_lifecycle_service


pytestmark = pytest.mark.skipif(
    os.getenv("CODESAGE_WORKER_ACCEPTANCE") != "1",
    reason="仅由一键验收启动真实依赖后运行",
)


def _start_worker(log_path: Path) -> tuple[subprocess.Popen, object]:
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "arq",
            "tests.worker_acceptance.acceptance_worker.WorkerSettings",
            "--burst",
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=os.environ.copy(),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return process, handle


async def _wait_for_stage(task_id: str, stage_type: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with async_session_factory() as db:
            stage = await db.scalar(select(AuditStageORM).where(
                AuditStageORM.task_id == task_id,
                AuditStageORM.stage_type == stage_type,
                AuditStageORM.status == "completed",
            ))
        if stage is not None:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"stage barrier timed out: {stage_type}")


async def _wait_for_result(redis: Redis, key: str, expected: str, timeout: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = await redis.hgetall(key)
        if record.get("result") == expected:
            return record
        await asyncio.sleep(0.1)
    raise TimeoutError(f"worker result barrier timed out: {expected}")


@pytest.mark.asyncio
@pytest.mark.parametrize("repetition", range(3))
async def test_cancel_then_resume_on_another_worker_keeps_completed_stage(repetition):
    suffix = uuid4().hex
    task_id = str(uuid4())
    user_id = str(uuid4())
    project_id = str(uuid4())
    workspace = (
        Path(settings.MANAGED_PROJECTS_ROOT)
        / ".auditai_workspaces" / "projects" / task_id
    )
    workspace.mkdir(parents=True, exist_ok=True)
    diff = workspace / "review.diff"
    diff.write_text(
        f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+cancel-resume-{repetition}\n",
        encoding="utf-8",
    )
    async with async_session_factory() as db:
        db.add(User(id=user_id, email=f"cancel-{suffix}@example.test", hashed_password="x"))
        db.add(Project(id=project_id, name=f"cancel-{suffix}", owner_id=user_id))
        db.add(AgentTask(
            id=task_id,
            project_id=project_id,
            created_by=user_id,
            version_label="acceptance",
            task_type="pr_review",
            status=AgentTaskStatus.PENDING,
            audit_scope={"pr_review": {"diff_file_path": str(diff)}},
        ))
        await db.commit()

    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    pool = await create_pool(
        RedisSettings.from_dsn(os.environ["REDIS_URL"]),
        default_queue_name=os.environ["AGENT_TASK_QUEUE_NAME"],
    )
    control_key = f"acceptance:control:{task_id}"
    record_key = f"acceptance:{task_id}"
    calls_key = f"acceptance:model_calls:{task_id}"
    await redis.delete(control_key, record_key, calls_key)
    await redis.hset(control_key, mapping={"block_non_security": "1"})
    logs = Path(os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"])
    processes: list[subprocess.Popen] = []
    handles: list[object] = []
    try:
        await pool.enqueue_job(
            "acceptance_execute", task_id, "cancel-first",
            _job_id=f"cancel-first:{task_id}",
        )
        process, handle = _start_worker(logs / f"cancel-{repetition}-first.log")
        processes.append(process)
        handles.append(handle)
        await _wait_for_stage(task_id, "review:security")
        async with async_session_factory() as db:
            before = await db.get(ReviewExecutionRun, task_id)
            original_run_id = (before.identity_json or {})["run_id"]
            original_attempt = before.attempt_id
            await task_lifecycle_service.cancel(db, task_id)
        first_record = await _wait_for_result(
            redis, record_key, AgentTaskStatus.CANCELLED, 10
        )
        first_worker = first_record["worker_id"]
        calls_before_resume = await redis.hgetall(calls_key)
        await asyncio.sleep(0.5)
        assert await redis.hgetall(calls_key) == calls_before_resume
        if process.poll() is None:
            process.terminate()
        await asyncio.to_thread(process.wait, 10)

        async with async_session_factory() as db:
            command = await task_lifecycle_service.resume(db, task_id)
            delivery = command.delivery_id
        await redis.hset(control_key, mapping={"release": "1"})
        await redis.delete(record_key)
        await pool.enqueue_job(
            "acceptance_execute", task_id, delivery,
            _job_id=f"cancel-resume:{task_id}",
        )
        process2, handle2 = _start_worker(logs / f"cancel-{repetition}-resume.log")
        processes.append(process2)
        handles.append(handle2)
        final_record = await _wait_for_result(
            redis, record_key, AgentTaskStatus.COMPLETED, 30
        )
        assert final_record["worker_id"] != first_worker
        calls_after_resume = await redis.hgetall(calls_key)
        assert calls_after_resume["review:security"] == calls_before_resume["review:security"]
        async with async_session_factory() as db:
            task = await db.get(AgentTask, task_id)
            row = await db.get(ReviewExecutionRun, task_id)
            assert task.status == AgentTaskStatus.COMPLETED
            assert (row.identity_json or {})["run_id"] == original_run_id
            assert row.attempt_id is None
            assert original_attempt is not None
            stages = (await db.execute(select(AuditStageORM).where(
                AuditStageORM.task_id == task_id
            ))).scalars().all()
            assert len(stages) == 5
            assert all(stage.status == "completed" for stage in stages)
            finding_rows = (await db.execute(select(AgentFinding).where(
                AgentFinding.task_id == task_id
            ))).scalars().all()
            assert {item.category for item in finding_rows} == {
                "security", "perf", "test_gap"
            }
    finally:
        for job_id in (f"cancel-first:{task_id}", f"cancel-resume:{task_id}"):
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
