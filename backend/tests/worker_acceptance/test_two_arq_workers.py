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
from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.audit_session import AuditSession, ToolExecutionReceipt
from app.models.checkpoint import AuditStageORM
from app.models.agent_task import AgentFinding, AgentTask, AgentTaskStatus
from app.models.project import Project
from app.models.user import User


pytestmark = pytest.mark.skipif(
    os.getenv("CODESAGE_WORKER_ACCEPTANCE") != "1",
    reason="仅由一键验收启动",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("repetition", range(3))
async def test_two_independent_arq_workers_execute_overlapping_tasks(tmp_path, repetition):
    suffix = uuid4().hex
    user_id = str(uuid4())
    project_id = str(uuid4())
    task_ids = [str(uuid4()), str(uuid4())]
    diff_paths = []
    for index, task_id in enumerate(task_ids):
        workspace = (
            Path(settings.MANAGED_PROJECTS_ROOT)
            / ".auditai_workspaces" / "projects" / task_id
        )
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "review.diff"
        path.write_text(
            f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+fixture {repetition}-{index}\n",
            encoding="utf-8",
        )
        diff_paths.append(path)

    async with async_session_factory() as db:
        db.add(User(id=user_id, email=f"workers-{suffix}@example.test", hashed_password="x"))
        db.add(Project(id=project_id, name=f"workers-{suffix}", owner_id=user_id))
        for task_id, diff_path in zip(task_ids, diff_paths):
            db.add(AgentTask(
                id=task_id,
                project_id=project_id,
                created_by=user_id,
                version_label="acceptance",
                task_type="pr_review",
                status=AgentTaskStatus.PENDING,
                audit_scope={"pr_review": {"diff_file_path": str(diff_path)}},
            ))
        await db.commit()

    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await redis.delete(
        *(f"acceptance:{task_id}" for task_id in task_ids),
        *(f"acceptance:model_calls:{task_id}" for task_id in task_ids),
        *(f"acceptance:control:{task_id}" for task_id in task_ids),
    )
    # 每轮同时覆盖规范非空结果与合法零发现结果。
    await redis.hset(
        f"acceptance:control:{task_ids[1]}", mapping={"zero_findings": "1"}
    )
    pool = await create_pool(
        RedisSettings.from_dsn(os.environ["REDIS_URL"]),
        default_queue_name=os.environ["AGENT_TASK_QUEUE_NAME"],
    )
    for index, task_id in enumerate(task_ids):
        await pool.enqueue_job(
            "acceptance_execute",
            task_id,
            f"delivery-{index}",
            _job_id=f"acceptance:{task_id}",
        )

    logs = Path(os.environ.get("CODESAGE_ACCEPTANCE_ARTIFACT_ROOT", str(tmp_path)))
    logs.mkdir(parents=True, exist_ok=True)
    processes = []
    handles = []
    try:
        for index in range(2):
            handle = (logs / f"worker-{repetition}-{index + 1}.log").open("w", encoding="utf-8")
            handles.append(handle)
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "arq", "tests.worker_acceptance.acceptance_worker.WorkerSettings", "--burst"],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=os.environ.copy(),
                stdout=handle,
                stderr=subprocess.STDOUT,
            ))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            records = [await redis.hgetall(f"acceptance:{task_id}") for task_id in task_ids]
            if all(record.get("ended") for record in records):
                break
            if any(process.poll() not in (None, 0) for process in processes):
                raise RuntimeError("ARQ worker exited unexpectedly; inspect worker-*.log")
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("independent ARQ workers did not finish")
        records = [await redis.hgetall(f"acceptance:{task_id}") for task_id in task_ids]
        assert len({record["worker_id"] for record in records}) == 2
        assert all(record["result"] == AgentTaskStatus.COMPLETED for record in records)
        starts = [float(record["started"]) for record in records]
        ends = [float(record["ended"]) for record in records]
        assert max(starts) < min(ends), "两个 worker 的执行区间必须重叠"
        for task_id in task_ids:
            calls = await redis.hgetall(f"acceptance:model_calls:{task_id}")
            assert calls == {
                "review:security": "2",
                "review:architecture": "2",
                "review:quality": "2",
            }
        async with async_session_factory() as db:
            for task_index, task_id in enumerate(task_ids):
                task = await db.get(AgentTask, task_id)
                assert task.status == AgentTaskStatus.COMPLETED
                stages = (await db.execute(
                    select(AuditStageORM).where(AuditStageORM.task_id == task_id)
                )).scalars().all()
                assert len(stages) == 5
                assert all(stage.status == "completed" for stage in stages)
                assert all((stage.state_payload or {}).get("stage_result") for stage in stages)
                finding_rows = (await db.execute(
                    select(AgentFinding).where(AgentFinding.task_id == task_id)
                )).scalars().all()
                expected_categories = (
                    {"security", "perf", "test_gap"} if task_index == 0 else set()
                )
                assert {row.category for row in finding_rows} == expected_categories
                assert all(row.vulnerability_type is None for row in finding_rows)
                session_count = await db.scalar(
                    select(func.count(AuditSession.id)).where(AuditSession.task_id == task_id)
                )
                assert session_count == 3
                read_calls = await db.scalar(
                    select(func.count(ToolExecutionReceipt.id))
                    .join(AuditSession, ToolExecutionReceipt.session_id == AuditSession.id)
                    .where(AuditSession.task_id == task_id, ToolExecutionReceipt.tool_name == "Read")
                )
                assert read_calls == 3
    finally:
        for task_id in task_ids:
            try:
                await Job(
                    f"acceptance:{task_id}", pool,
                    _queue_name=os.environ["AGENT_TASK_QUEUE_NAME"],
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
