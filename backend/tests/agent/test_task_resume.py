from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.api.v1.endpoints.agent_tasks as endpoint
from app.api.v1.endpoints.agent_tasks import resume_agent_task
from app.models.agent_task import AgentTask, AgentTaskPhase, AgentTaskStatus
from app.models.project import Project


class _FakeDB:
    def __init__(self, task, project):
        self.task = task
        self.project = project

    async def get(self, model, key, options=None):
        del key, options
        return self.task if model is AgentTask else self.project if model is Project else None


@pytest.mark.asyncio
async def test_resume_api_delegates_to_lifecycle_then_schedules(monkeypatch):
    task = AgentTask(
        id="task-1", project_id="project-1", created_by="user-1",
        name="Demo", version_label="resume-test",
        status=AgentTaskStatus.RUNNING, current_phase=AgentTaskPhase.PLANNING,
        current_step="Resume from checkpoint",
    )
    db = _FakeDB(
        task,
        Project(id="project-1", name="Demo Project", owner_id="user-1", source_type="repository"),
    )
    resume = AsyncMock(return_value=SimpleNamespace(task=task, delivery_id="delivery-test"))
    schedule = AsyncMock()
    monkeypatch.setattr(
        "app.control_plane.lifecycle.task_lifecycle_service.resume", resume
    )
    monkeypatch.setattr(endpoint, "_schedule_agent_task", schedule)

    response = await resume_agent_task(
        "task-1", db=db,
        current_user=SimpleNamespace(id="user-1"),
    )

    assert response["status"] == AgentTaskStatus.RUNNING
    resume.assert_awaited_once_with(db, "task-1")
    schedule.assert_awaited_once_with("task-1", delivery_id="delivery-test")


@pytest.mark.asyncio
async def test_resume_api_maps_invalid_state(monkeypatch):
    task = AgentTask(
        id="task-1", project_id="project-1", created_by="user-1",
        name="Demo", version_label="resume-test", status=AgentTaskStatus.COMPLETED,
    )
    db = _FakeDB(
        task,
        Project(id="project-1", name="Demo Project", owner_id="user-1", source_type="repository"),
    )
    from app.control_plane.lifecycle import InvalidTaskStateError

    monkeypatch.setattr(
        "app.control_plane.lifecycle.task_lifecycle_service.resume",
        AsyncMock(side_effect=InvalidTaskStateError("Task is not resumable")),
    )
    with pytest.raises(HTTPException) as captured:
        await resume_agent_task(
            "task-1", db=db,
            current_user=SimpleNamespace(id="user-1"),
        )
    assert captured.value.status_code == 400
