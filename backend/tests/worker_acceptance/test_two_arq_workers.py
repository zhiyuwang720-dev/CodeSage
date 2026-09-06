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
from redis.asyncio import Redis

from app.db.session import async_session_factory
from app.models.agent_task import AgentTask, AgentTaskStatus
from app.models.project import Project
from app.models.user import User


pytestmark = pytest.mark.skipif(
    os.getenv("CODESAGE_WORKER_ACCEPTANCE") != "1",
    reason="仅由一键验收启动",
)


@pytest.mark.asyncio
async def test_two_independent_arq_workers_execute_overlapping_tasks(tmp_path):
    suffix = uuid4().hex
    user_id = str(uuid4())
    project_id = str(uuid4())
    task_ids = [str(uuid4()), str(uuid4())]
    diff_paths = []
    for index in range(2):
        path = tmp_path / f"review-{index}.diff"
        path.write_text(f"diff --git a/a.py b/a.py\n+fixture {index}\n", encoding="utf-8")
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
    await redis.delete("acceptance:barrier", *(f"acceptance:{task_id}" for task_id in task_ids))
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
            handle = (logs / f"worker-{index + 1}.log").open("w", encoding="utf-8")
            handles.append(handle)
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "arq", "tests.worker_acceptance.acceptance_worker.WorkerSettings", "--burst"],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=os.environ.copy(),
                stdout=handle,
                stderr=subprocess.STDOUT,
            ))
        deadline = time.monotonic() + 30
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
        assert all(record["tool_success"] == "1" for record in records)
        starts = [float(record["started"]) for record in records]
        ends = [float(record["ended"]) for record in records]
        assert max(starts) < min(ends), "两个 worker 的执行区间必须重叠"
    finally:
        await pool.close()
        await redis.aclose()
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        for handle in handles:
            handle.close()
