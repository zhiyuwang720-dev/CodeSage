import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agent.task_queue import AGENT_TASK_JOB_NAME, AgentTaskQueue, should_use_worker_queue


class _FakeArqPool:
    def __init__(self):
        self.jobs = []
        self.closed = False

    async def enqueue_job(self, function, *args, **kwargs):
        self.jobs.append((function, args, kwargs))
        return object()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_agent_task_queue_enqueues_task_payload():
    pool = _FakeArqPool()
    queue = AgentTaskQueue(arq_pool=pool, queue_name="agent:q")

    await queue.enqueue("task-1")

    assert pool.jobs == [
        (
            AGENT_TASK_JOB_NAME,
            ("task-1",),
            {"_job_id": "agent-task:task-1", "_queue_name": "agent:q"},
        )
    ]


def test_should_use_worker_queue_only_for_worker_mode(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_TASK_EXECUTION_MODE", "worker")
    assert should_use_worker_queue() is True

    monkeypatch.setattr(settings, "AGENT_TASK_EXECUTION_MODE", "inline")
    assert should_use_worker_queue() is False
