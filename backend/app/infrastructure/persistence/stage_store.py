"""09: `audit_stages` 表读写实现(AuditStageStore)。

所有写按 (task_id, stage_type) 幂等 upsert: 重跑/恢复不产生重复行。
对外返回契约层 `AuditStage`(Pydantic), 执行器/resume 消费方直接读 state_payload 快照。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.models.checkpoint import AuditStageORM
from app.models.review_execution import ReviewExecutionRun
from app.contracts.checkpoint import AuditStage, StageStatus
from app.contracts.review_execution import ArtifactRef, StageResult
from app.control_plane.execution_ownership import (
    guard_managed_execution_write,
)

logger = logging.getLogger(__name__)


async def _guard_managed_write(db, task_id: str) -> None:
    await guard_managed_execution_write(db, task_id)


def _stage_id(task_id: str, stage_type: str) -> str:
    return f"{task_id}:{stage_type}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_contract(row: AuditStageORM) -> AuditStage:
    try:
        status = StageStatus(row.status)
    except ValueError:
        status = StageStatus.pending
    return AuditStage(
        stage_id=row.stage_id,
        task_id=row.task_id,
        stage_type=row.stage_type,
        status=status,
        session_id=row.session_id,
        turn_count=row.turn_count or 0,
        token_usage=row.token_usage or 0,
        tool_calls=row.tool_calls or 0,
        findings_count=row.findings_count or 0,
        state_payload=dict(row.state_payload or {}),
        error_message=row.error_message,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


async def _load(db, task_id: str, stage_type: str) -> AuditStageORM | None:
    result = await db.execute(
        select(AuditStageORM).where(
            AuditStageORM.task_id == task_id,
            AuditStageORM.stage_type == stage_type,
        )
    )
    return result.scalar_one_or_none()


def _new_row(task_id: str, stage_type: str) -> AuditStageORM:
    return AuditStageORM(
        task_id=task_id,
        stage_id=_stage_id(task_id, stage_type),
        stage_type=stage_type,
        status=StageStatus.pending.value,
    )


class AuditStageStoreImpl:
    """SQLAlchemy 版 AuditStageStore。db 为 AsyncSession(主库)。"""

    async def register(self, db, task_id: str, planned_stages: list[str]) -> None:
        """任务启动登记 pending(幂等): 已存在记录跳过, 不覆盖进度。"""
        await _guard_managed_write(db, task_id)
        for stage_type in planned_stages:
            if await _load(db, task_id, stage_type) is not None:
                continue
            db.add(_new_row(task_id, stage_type))
        await db.commit()

    async def start(
        self, db, task_id: str, stage_type: str, *, session_id: str | None = None
    ) -> AuditStage:
        await _guard_managed_write(db, task_id)
        row = await _load(db, task_id, stage_type)
        if row is None:
            row = _new_row(task_id, stage_type)
            db.add(row)
        elif row.status == StageStatus.completed.value:
            # A late/replayed session_start must never downgrade reusable work.
            return _to_contract(row)
        row.status = StageStatus.running.value
        row.started_at = row.started_at or _now()
        if session_id:
            row.session_id = session_id
        await db.commit()
        return _to_contract(row)

    async def complete(
        self,
        db,
        task_id: str,
        stage_type: str,
        *,
        session_id: str | None = None,
        stats: dict[str, Any] | None = None,
        findings: list[dict[str, Any]] | None = None,
        payload: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> AuditStage:
        await _guard_managed_write(db, task_id)
        row = await _load(db, task_id, stage_type)
        if row is None:
            row = _new_row(task_id, stage_type)
            db.add(row)
        row.status = StageStatus.completed.value
        row.completed_at = _now()
        row.error_message = None
        if session_id:
            row.session_id = session_id
        stats = dict(stats or {})
        row.turn_count = int(stats.get("turn_count") or row.turn_count or 0)
        row.token_usage = int(stats.get("token_usage") or row.token_usage or 0)
        row.tool_calls = int(stats.get("tool_calls") or row.tool_calls or 0)
        if findings is not None:
            row.findings_count = len(findings)
        new_payload = dict(payload or {})
        if findings is not None:
            new_payload["findings"] = findings
        execution = await db.get(ReviewExecutionRun, task_id)
        if execution is not None:
            run_id = str((execution.identity_json or {}).get("run_id") or "")
            artifact_refs = [
                ArtifactRef.model_validate(item)
                for item in (new_payload.get("artifact_refs") or [])
            ]
            diagnostic_stats = {
                key: value
                for key, value in stats.items()
                if key not in {"turn_count", "token_usage", "tool_calls", "findings_count"}
            }
            stage_result = StageResult(
                run_id=run_id,
                stage_type=stage_type,
                status="completed",
                session_id=row.session_id,
                findings=findings or [],
                artifact_refs=artifact_refs,
                stats={
                    **diagnostic_stats,
                    "turn_count": row.turn_count,
                    "token_usage": row.token_usage,
                    "tool_calls": row.tool_calls,
                    "findings_count": row.findings_count,
                },
            )
            new_payload["stage_result"] = stage_result.model_dump(mode="json")
        row.state_payload = new_payload
        if commit:
            await db.commit()
        else:
            await db.flush()
        return _to_contract(row)

    async def fail(self, db, task_id: str, stage_type: str, error: str) -> AuditStage:
        await _guard_managed_write(db, task_id)
        row = await _load(db, task_id, stage_type)
        if row is None:
            row = _new_row(task_id, stage_type)
            db.add(row)
        row.status = StageStatus.failed.value
        row.error_message = str(error)
        row.completed_at = row.completed_at or _now()
        execution = await db.get(ReviewExecutionRun, task_id)
        if execution is not None:
            run_id = str((execution.identity_json or {}).get("run_id") or "")
            payload = dict(row.state_payload or {})
            payload["stage_result"] = StageResult(
                run_id=run_id,
                stage_type=stage_type,
                status="failed",
                session_id=row.session_id,
                stats={
                    "turn_count": row.turn_count or 0,
                    "token_usage": row.token_usage or 0,
                    "tool_calls": row.tool_calls or 0,
                    "findings_count": row.findings_count or 0,
                },
                error_code="stage_failed",
                error_message=str(error),
            ).model_dump(mode="json")
            row.state_payload = payload
        await db.commit()
        return _to_contract(row)

    async def list(self, db, task_id: str) -> list[AuditStage]:
        result = await db.execute(
            select(AuditStageORM)
            .where(AuditStageORM.task_id == task_id)
            .order_by(AuditStageORM.stage_type)
        )
        return [_to_contract(r) for r in result.scalars().all()]

    async def completed(self, db, task_id: str) -> list[AuditStage]:
        result = await db.execute(
            select(AuditStageORM).where(
                AuditStageORM.task_id == task_id,
                AuditStageORM.status == StageStatus.completed.value,
            )
        )
        return [_to_contract(r) for r in result.scalars().all()]

    async def get(self, db, task_id: str, stage_type: str) -> AuditStage | None:
        row = await _load(db, task_id, stage_type)
        return _to_contract(row) if row is not None else None

    async def touch_session(
        self, db, task_id: str, stage_type: str, session_id: str
    ) -> None:
        """session_start 事件更新 L3 会话锚点(进程死在对话中途时 resume 靠它续跑)。"""
        await _guard_managed_write(db, task_id)
        row = await _load(db, task_id, stage_type)
        if row is None:
            row = _new_row(task_id, stage_type)
            row.status = StageStatus.running.value
            db.add(row)
        row.session_id = session_id
        await db.commit()


# 进程内共享单例: 执行器/sink/resume 消费统一走它。
audit_stage_store: AuditStageStore = AuditStageStoreImpl()
