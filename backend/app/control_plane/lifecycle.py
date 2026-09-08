"""快速审查 start/cancel/resume 生命周期命令。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.core.config import settings
from app.models.agent_task import AgentTask, AgentTaskPhase, AgentTaskStatus
from app.models.review_execution import ReviewExecutionRun
from app.control_plane.review_policy import build_review_compatibility_config
from app.control_plane.execution_ownership import review_execution_ownership
from app.control_plane.review_inputs import initialize_or_resume_review_input


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
    async def start(self, db, task_id: str) -> LifecycleCommandResult:
        task = await _locked_task(db, task_id)
        if task.task_type != "pr_review" and not (task.audit_scope or {}).get("pr_review"):
            raise InvalidTaskStateError("Only pr_review tasks can be started")
        if task.status != AgentTaskStatus.PENDING:
            raise InvalidTaskStateError(f"Task already started (status={task.status})")
        delivery_id = str(uuid4())
        await initialize_or_resume_review_input(
            db,
            task,
            compatibility_config=build_review_compatibility_config(task),
            artifact_root=Path(settings.MANAGED_PROJECTS_ROOT) / ".review_artifacts",
            delivery_id=delivery_id,
        )
        task.status = AgentTaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        task.current_phase = AgentTaskPhase.PLANNING
        await db.commit()
        await db.refresh(task)
        return LifecycleCommandResult(task, delivery_id)

    async def resume(self, db, task_id: str) -> LifecycleCommandResult:
        # 控制命令统一 execution row -> task row 锁顺序，避免 cancel/resume 互锁。
        if await _locked_execution(db, task_id) is None:
            raise InvalidTaskStateError("任务缺少可恢复的执行身份")
        task = await _locked_task(db, task_id)
        if task.status in (AgentTaskStatus.RUNNING, AgentTaskStatus.COMPLETED):
            raise InvalidTaskStateError(f"Task is not resumable (status={task.status})")
        if task.status not in (
            AgentTaskStatus.CANCELLED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.PAUSED,
        ):
            raise InvalidTaskStateError(f"Task is not resumable (status={task.status})")
        # 与执行入口共用 inputs.py 单点：校验当前声明、既有身份和可信输入引用。
        prepared = await initialize_or_resume_review_input(
            db,
            task,
            compatibility_config=build_review_compatibility_config(task),
            artifact_root=Path(settings.MANAGED_PROJECTS_ROOT) / ".review_artifacts",
            delivery_id=str(uuid4()),
        )
        del prepared
        delivery_id = await review_execution_ownership.prepare_resume(
            db, task_id, commit=False
        )
        task.status = AgentTaskStatus.RUNNING
        task.current_phase = AgentTaskPhase.PLANNING
        task.current_step = "Resume from checkpoint"
        task.completed_at = None
        task.error_message = None
        config = dict(task.agent_config or {})
        config["resume_from_checkpoint"] = True
        task.agent_config = config
        await db.commit()
        await db.refresh(task)
        return LifecycleCommandResult(task, delivery_id)

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
