from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

import app.api.v1.endpoints.agent_tasks as agent_tasks_endpoint
from app.api.v1.endpoints.agent_tasks import (
    _save_findings,
    resume_agent_task,
)
from app.models.agent_task import AgentTask, AgentTaskPhase, AgentTaskStatus
from app.models.project import Project


class _FakeDB:
    def __init__(self, task, project):
        self._task = task
        self._project = project
        self.commit = AsyncMock()

    async def get(self, model, key, options=None):
        del options
        if model is AgentTask and key == self._task.id:
            return self._task
        if model is Project and key == self._project.id:
            return self._project
        return None


@pytest.mark.asyncio
async def test_resume_agent_task_resets_task_and_schedules_background_execution():
    task = AgentTask(
        id="task-1",
        project_id="project-1",
        created_by="user-1",
        name="Demo",
        version_label="resume-test",
        status=AgentTaskStatus.CANCELLED,
        current_phase=AgentTaskPhase.ANALYSIS,
        error_message="Task cancelled",
    )
    project = Project(id="project-1", name="Demo Project", owner_id="user-1", source_type="repository")
    db = _FakeDB(task, project)
    background_tasks = BackgroundTasks()

    response = await resume_agent_task(
        task_id="task-1",
        background_tasks=background_tasks,
        db=db,
        current_user=SimpleNamespace(id="user-1"),
    )

    assert response["message"] == "Task resumed"
    assert response["task_id"] == "task-1"
    assert task.status == AgentTaskStatus.PENDING
    assert task.current_phase == AgentTaskPhase.PLANNING
    assert task.error_message is None
    assert task.current_step == "Resuming from latest checkpoint"
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].func is agent_tasks_endpoint._execute_agent_task
    assert background_tasks.tasks[0].args == ("task-1",)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_agent_task_rejects_completed_tasks():
    task = AgentTask(
        id="task-1",
        project_id="project-1",
        created_by="user-1",
        name="Demo",
        version_label="resume-test",
        status=AgentTaskStatus.COMPLETED,
    )
    project = Project(id="project-1", name="Demo Project", owner_id="user-1", source_type="repository")
    db = _FakeDB(task, project)

    with pytest.raises(HTTPException) as exc_info:
        await resume_agent_task(
            task_id="task-1",
            background_tasks=BackgroundTasks(),
            db=db,
            current_user=SimpleNamespace(id="user-1"),
        )

    assert exc_info.value.status_code == 400


class _FakeFindingScalars:
    def __init__(self, findings):
        self._findings = findings

    def all(self):
        return list(self._findings)


class _FakeFindingResult:
    def __init__(self, findings):
        self._findings = findings

    def scalars(self):
        return _FakeFindingScalars(self._findings)


class _FakeFindingDB:
    def __init__(self, existing_findings=None):
        self._existing_findings = list(existing_findings or [])
        self.added = []
        self.commit = AsyncMock()

    async def execute(self, stmt):
        assert stmt is not None
        return _FakeFindingResult(self._existing_findings)

    def add(self, record):
        self.added.append(record)


def _build_existing_finding(*, fingerprint: str | None = None, verified: bool = False):
    finding = agent_tasks_endpoint.AgentFinding(
        id='finding-1',
        task_id='task-1',
        vulnerability_type='sql_injection',
        severity='medium',
        title='SQL injection in api.py',
        file_path='src/api.py',
        line_start=21,
        line_end=21,
        code_snippet='cursor.execute(query)',
        status='verified' if verified else 'new',
        is_verified=verified,
        ai_confidence=0.4,
        verification_result={'verdict': 'confirmed' if verified else 'candidate'},
        finding_metadata={'raw_finding': {'title': 'SQL injection in api.py'}},
    )
    finding.fingerprint = fingerprint or finding.generate_fingerprint()
    return finding


@pytest.mark.asyncio
async def test_save_findings_merges_duplicate_resume_finding_instead_of_reinserting():
    existing = _build_existing_finding()
    db = _FakeFindingDB(existing_findings=[existing])

    persisted = await _save_findings(
        db,
        'task-1',
        [
            {
                'title': 'SQL injection in api.py',
                'vulnerability_type': 'sql_injection',
                'severity': 'high',
                'file_path': 'src/api.py',
                'line_start': 21,
                'line_end': 21,
                'code_snippet': 'cursor.execute(query)',
                'confidence': 0.9,
                'verdict': 'confirmed',
                'verification_method': 'resume-checkpoint',
                'verification_result': {'verdict': 'confirmed'},
                'references': ['https://example.com/advisory'],
            }
        ],
    )

    assert persisted == 1
    assert db.added == []
    assert existing.is_verified is True
    assert existing.status == 'verified'
    assert existing.severity == 'high'
    assert existing.ai_confidence == 0.9
    assert existing.verification_method == 'resume-checkpoint'
    assert existing.references == ['https://example.com/advisory']
    assert existing.finding_metadata['merge_count'] == 1
    db.commit.assert_awaited_once()
