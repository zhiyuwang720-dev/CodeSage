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

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.agent_task import AgentTask, AgentTaskStatus
from app.models.project import Project
from app.models.user import User

pytestmark = pytest.mark.skipif(
    os.getenv("CODESAGE_WORKER_ACCEPTANCE") != "1", reason="仅由一键验收启动"
)


@pytest.mark.asyncio
async def test_production_timeout_allows_healthy_task_over_sixty_seconds(tmp_path):
    task_id, project_id, user_id = str(uuid4()), str(uuid4()), str(uuid4())
    workspace = Path(settings.MANAGED_PROJECTS_ROOT) / ".auditai_workspaces" / "projects" / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    diff = workspace / "review.diff"
    diff.write_text("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+healthy\n", encoding="utf-8")
    async with async_session_factory() as db:
        db.add(User(id=user_id, email=f"long-{task_id}@example.test", hashed_password="x"))
        db.add(Project(id=project_id, name=f"long-{task_id}", owner_id=user_id))
        db.add(AgentTask(id=task_id, project_id=project_id, created_by=user_id,
                         version_label="acceptance", task_type="pr_review",
                         status=AgentTaskStatus.PENDING,
                         audit_scope={"pr_review": {"diff_file_path": str(diff)}}))
        await db.commit()

    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await redis.hset(f"acceptance:control:{task_id}", mapping={"healthy_delay_seconds": "61"})
    pool = await create_pool(RedisSettings.from_dsn(os.environ["REDIS_URL"]),
                             default_queue_name=os.environ["AGENT_TASK_QUEUE_NAME"])
    await pool.enqueue_job("acceptance_execute", task_id, "delivery-long",
                           _job_id=f"acceptance:long:{task_id}")
    log = Path(os.environ.get("CODESAGE_ACCEPTANCE_ARTIFACT_ROOT", str(tmp_path))) / "worker-long.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CODESAGE_ACCEPTANCE_JOB_TIMEOUT"] = "3600"
    started = time.monotonic()
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "arq", "tests.worker_acceptance.acceptance_worker.WorkerSettings", "--burst"],
            cwd=str(Path(__file__).resolve().parents[2]), env=env,
            stdout=handle, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if (await redis.hget(f"acceptance:{task_id}", "ended")):
                    break
                if process.poll() not in (None, 0):
                    raise RuntimeError(f"worker exited; inspect {log}")
                await asyncio.sleep(0.2)
            else:
                raise TimeoutError("healthy long-running task did not finish")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            await pool.close()
            await redis.aclose()

    assert time.monotonic() - started > 60
    async with async_session_factory() as db:
        assert (await db.get(AgentTask, task_id)).status == AgentTaskStatus.COMPLETED
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        assert await redis.hget(f"acceptance:executor_calls:{task_id}", "count") == "1"
    finally:
        await redis.aclose()
