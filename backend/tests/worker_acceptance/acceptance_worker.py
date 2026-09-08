"""仅供本地验收命令使用的确定性模型 ARQ worker。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from uuid import uuid4

from arq.connections import RedisSettings
from arq.worker import func

from app.execution_plane.review.execution import QuickReviewDependencies, execute_quick_review
from app.infrastructure.messaging.task_queue import AGENT_TASK_JOB_NAME
from app.worker.agent_worker import execute_agent_task_job


class DeterministicReviewModel:
    """通过正常 LLMService 接口驱动真实 QueryLoop；不访问外部模型。"""

    def __init__(self, redis, task_id: str):
        self.redis = redis
        self.task_id = task_id
        self.calls = defaultdict(int)

    async def chat_completion(
        self, messages, temperature=None, max_tokens=None, agent_type=None,
        tools=None, parallel_tool_calls=None,
    ):
        del messages, temperature, max_tokens, tools, parallel_tool_calls
        perspective = str(agent_type or "unknown")
        self.calls[perspective] += 1
        await self.redis.hincrby(f"acceptance:model_calls:{self.task_id}", perspective, 1)
        if (
            perspective != "review:security"
            and await self.redis.hget(f"acceptance:control:{self.task_id}", "block_non_security")
        ):
            while not await self.redis.hget(
                f"acceptance:control:{self.task_id}", "release"
            ):
                await asyncio.sleep(0.05)
        if self.calls[perspective] == 1:
            payload = {"file_path": "review.diff", "start_line": 1, "max_lines": 20}
            tool_name = "Read"
        else:
            zero_findings = await self.redis.hget(
                f"acceptance:control:{self.task_id}", "zero_findings"
            )
            perspective_name = perspective.removeprefix("review:")
            categories = {
                "security": "security",
                "architecture": "perf",
                "quality": "test_gap",
            }
            findings = [] if zero_findings else [{
                "rule_id": f"FIXTURE-{perspective_name.upper()}",
                "severity": "medium",
                "category": categories[perspective_name],
                "title": f"deterministic {categories[perspective_name]} finding",
                "description": "deterministic acceptance evidence",
                "file_path": "a.py",
                "line_start": 1,
                "line_end": 1,
                "suggestion": "add a regression guard",
                "confidence": 0.9,
                "needs_verification": False,
                "verdict": "confirmed",
                "source": perspective_name,
            }]
            payload = {
                "findings": findings,
                "summary": f"{perspective} fixture review completed after reading review.diff",
            }
            tool_name = "FinalizeReview"
        return {
            "content": "",
            "reasoning_content": "",
            "finish_reason": "tool_calls",
            "usage": {"total_tokens": 10, "input_tokens": 6, "output_tokens": 4},
            "tool_calls": [{
                "id": f"call-{uuid4().hex[:10]}",
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(payload)},
            }],
        }


async def execute_acceptance_review(redis, task_id: str, delivery_id: str | None) -> str:
    worker_id = f"acceptance:{os.getpid()}"
    print(f"worker_id={worker_id} task_id={task_id}", flush=True)
    await redis.hset(
        f"acceptance:{task_id}",
        mapping={"worker_id": worker_id, "started": str(time.time())},
    )
    await redis.hincrby(f"acceptance:executor_calls:{task_id}", "count", 1)
    delay = await redis.hget(f"acceptance:control:{task_id}", "healthy_delay_seconds")
    if delay:
        await asyncio.sleep(float(delay))
    async def observe(event: dict) -> None:
        await redis.rpush(
            f"acceptance:attempts:{task_id}", json.dumps(event, sort_keys=True)
        )

    result = await execute_quick_review(
        task_id,
        QuickReviewDependencies(
            llm_service=DeterministicReviewModel(redis, task_id),
            worker_id=worker_id,
            artifact_root=os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"],
            observer=observe,
        ),
        delivery_id=delivery_id,
    )
    await redis.hset(
        f"acceptance:{task_id}",
        mapping={"ended": str(time.time()), "result": result},
    )
    return result


async def startup(ctx) -> None:
    async def executor(task_id: str, delivery_id: str | None = None) -> str:
        return await execute_acceptance_review(ctx["redis"], task_id, delivery_id)

    ctx["execute_agent_task"] = executor


class WorkerSettings:
    functions = [func(execute_agent_task_job, name="acceptance_execute")]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
    queue_name = os.environ["AGENT_TASK_QUEUE_NAME"]
    max_jobs = 1
    # Fault-takeover suites use 60 explicitly; the healthy-long-task suite uses
    # the production profile and proves 60 seconds is not a normal execution cap.
    job_timeout = int(os.getenv("CODESAGE_ACCEPTANCE_JOB_TIMEOUT", "60"))
    max_tries = 20
