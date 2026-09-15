"""Product-neutral task lifecycle state transitions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.agent_task import AgentTask, AgentTaskStatus
from app.control_plane.persistence.execution_models import ReviewExecutionRun


class TaskLifecycleError(RuntimeError):
    pass


class TaskNotFoundError(TaskLifecycleError):
    pass


class InvalidTaskStateError(TaskLifecycleError):
    pass


@dataclass(frozen=True)
class LifecycleCommandResult:
    task: AgentTask
    delivery_id: str


async def _locked_task(db, task_id: str) -> AgentTask:
    result = await db.execute(
        select(AgentTask).where(AgentTask.id == task_id).with_for_update()
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise TaskNotFoundError(f"Task not found: {task_id}")
    return task


async def _locked_execution(db, task_id: str) -> ReviewExecutionRun | None:
    result = await db.execute(
        select(ReviewExecutionRun)
        .where(ReviewExecutionRun.task_id == task_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


class TaskLifecycleService:
    async def cancel(self, db, task_id: str) -> AgentTask:
        execution = await _locked_execution(db, task_id)
        task = await _locked_task(db, task_id)
        if task.status in (
            AgentTaskStatus.COMPLETED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.CANCELLED,
        ):
            raise InvalidTaskStateError("Task is already finished")
        if execution is not None:
            execution.cancel_requested = True
        task.status = AgentTaskStatus.CANCELLED
        task.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(task)
        return task


task_lifecycle_service = TaskLifecycleService()
