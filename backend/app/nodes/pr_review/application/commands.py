"""PR review start and resume commands."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.config import settings
from app.models.agent_task import AgentTaskPhase, AgentTaskStatus
from app.control_plane.lifecycle import (
    InvalidTaskStateError,
    LifecycleCommandResult,
    _locked_execution,
    _locked_task,
)
from app.control_plane.scale_ops.ownership import review_execution_ownership
from app.nodes.pr_review.application.inputs import initialize_or_resume_review_input
from app.nodes.pr_review.application.policy import build_review_compatibility_config


class PrReviewLifecycleService:
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
        await initialize_or_resume_review_input(
            db,
            task,
            compatibility_config=build_review_compatibility_config(task),
            artifact_root=Path(settings.MANAGED_PROJECTS_ROOT) / ".review_artifacts",
            delivery_id=str(uuid4()),
        )
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


pr_review_lifecycle_service = PrReviewLifecycleService()

