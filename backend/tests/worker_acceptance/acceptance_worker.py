"""仅供本地验收命令使用的确定性 ARQ worker。"""
from __future__ import annotations

import asyncio
import os
import time

from arq.connections import RedisSettings
from arq.worker import func

from app.db.session import async_session_factory
from app.models.agent_task import AgentTask
from app.services.contracts.tools import ToolExecutionContext
from app.services.pr_review.execution import QuickReviewDependencies, execute_quick_review
from app.services.tooling.read import ReadRuntimeTool


async def execute_acceptance_review(ctx, task_id: str, delivery_id: str) -> str:
    redis = ctx["redis"]
    worker_id = f"acceptance:{os.getpid()}"
    print(f"worker_id={worker_id} task_id={task_id}", flush=True)

    async def deterministic_runner(inner_task_id: str) -> None:
        async with async_session_factory() as db:
            task = await db.get(AgentTask, inner_task_id)
            diff_path = ((task.audit_scope or {}).get("pr_review") or {})["diff_file_path"]
        tool = ReadRuntimeTool(project_root=os.path.dirname(diff_path))
        payload = await tool.execute(
            tool.validate_input({"file_path": os.path.basename(diff_path)}),
            ToolExecutionContext(
                session_id=f"session-{inner_task_id}",
                turn_id="turn-1",
                tool_use_id="read-1",
                tool_call_id="read-1",
            ),
        )
        if payload.is_error:
            raise RuntimeError(payload.content)
        await redis.hset(f"acceptance:{inner_task_id}", mapping={
            "worker_id": worker_id,
            "started": str(time.time()),
            "tool_success": "1",
        })
        await redis.incr("acceptance:barrier")
        deadline = time.monotonic() + 15
        while int(await redis.get("acceptance:barrier") or 0) < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("two-worker synchronization barrier timed out")
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.25)
        await redis.hset(f"acceptance:{inner_task_id}", mapping={"ended": str(time.time())})

    return await execute_quick_review(
        task_id,
        QuickReviewDependencies(
            runner=deterministic_runner,
            worker_id=worker_id,
            artifact_root=os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"],
        ),
        delivery_id=delivery_id,
    )


class WorkerSettings:
    functions = [func(execute_acceptance_review, name="acceptance_execute")]
    redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
    queue_name = os.environ["AGENT_TASK_QUEUE_NAME"]
    max_jobs = 1
    job_timeout = 30
    max_tries = 1
