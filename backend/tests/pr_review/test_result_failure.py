from types import SimpleNamespace

import pytest

from app.control_plane.results import ReviewResultService
from app.models.agent_task import AgentTaskPhase, AgentTaskStatus


@pytest.mark.asyncio
async def test_mark_failed_rolls_back_before_loading_by_stable_task_id():
    events: list[str] = []
    task = SimpleNamespace(
        status=AgentTaskStatus.RUNNING,
        current_phase=AgentTaskPhase.ANALYSIS,
        completed_at=None,
        error_message=None,
    )

    class FakeSession:
        async def rollback(self):
            events.append("rollback")

        async def get(self, model, task_id):
            assert events == ["rollback"]
            assert task_id == "task-1"
            events.append("get")
            return task

        async def commit(self):
            events.append("commit")

    await ReviewResultService().mark_failed(
        FakeSession(), "task-1", ValueError("original validation error")
    )

    assert events == ["rollback", "get", "commit"]
    assert task.status == AgentTaskStatus.FAILED
    assert task.current_phase == AgentTaskPhase.REPORTING
    assert task.completed_at is not None
    assert task.error_message == "original validation error"
