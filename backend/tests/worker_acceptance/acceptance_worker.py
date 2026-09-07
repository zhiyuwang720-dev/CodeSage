"""仅供本地验收命令使用的确定性模型 ARQ worker。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from uuid import uuid4

from arq.connections import RedisSettings
from arq.worker import Retry, func

from app.services.pr_review.execution import QuickReviewDependencies, execute_quick_review


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
            payload = {
                "findings": [],
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


async def execute_acceptance_review(ctx, task_id: str, delivery_id: str) -> str:
    redis = ctx["redis"]
    worker_id = f"acceptance:{os.getpid()}"
    print(f"worker_id={worker_id} task_id={task_id}", flush=True)
    await redis.hset(
        f"acceptance:{task_id}",
        mapping={"worker_id": worker_id, "started": str(time.time())},
    )
    result = await execute_quick_review(
        task_id,
        QuickReviewDependencies(
            llm_service=DeterministicReviewModel(redis, task_id),
            worker_id=worker_id,
            artifact_root=os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"],
        ),
        delivery_id=delivery_id,
    )
    if result == "already_owned":
        raise Retry(defer=2)
    await redis.hset(
        f"acceptance:{task_id}",
        mapping={"ended": str(time.time()), "result": result},
    )
    return result


class WorkerSettings:
    functions = [func(execute_acceptance_review, name="acceptance_execute")]
    redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
    queue_name = os.environ["AGENT_TASK_QUEUE_NAME"]
    max_jobs = 1
    job_timeout = 60
    max_tries = 20
