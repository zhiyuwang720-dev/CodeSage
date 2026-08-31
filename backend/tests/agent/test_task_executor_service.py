import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agent import task_executor


class _FakeRunner:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeAsyncioTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


def test_request_agent_task_cancellation_uses_service_runtime_registries():
    runner = _FakeRunner()
    asyncio_task = _FakeAsyncioTask()
    task_executor._running_tasks["task-1"] = runner
    task_executor._running_asyncio_tasks["task-1"] = asyncio_task

    try:
        task_executor.request_agent_task_cancellation("task-1")

        assert "task-1" in task_executor._cancelled_tasks
        assert runner.cancelled is True
        assert asyncio_task.cancelled is True
    finally:
        task_executor._running_tasks.pop("task-1", None)
        task_executor._running_asyncio_tasks.pop("task-1", None)
        task_executor._cancelled_tasks.discard("task-1")


@pytest.mark.asyncio
async def test_watch_task_cancellation_cancels_running_task_when_db_status_is_cancelled(monkeypatch):
    class _FakeDbTask:
        status = "cancelled"

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, model, task_id):
            return _FakeDbTask()

    class _FakeSessionFactory:
        def __call__(self):
            return _FakeSession()

    running_task = _FakeAsyncioTask()

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(task_executor.asyncio, "sleep", no_sleep)

    await task_executor._watch_task_cancellation(
        task_id="task-1",
        run_task=running_task,
        session_factory=_FakeSessionFactory(),
        poll_interval=0,
    )

    assert running_task.cancelled is True
    assert "task-1" in task_executor._cancelled_tasks
    task_executor._cancelled_tasks.discard("task-1")
