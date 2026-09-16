"""PostgreSQL 行锁驱动的任务级执行所有权。"""
from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from opentelemetry import trace
from sqlalchemy import func, select, update

from app.models.agent_task import AgentTask, AgentTaskStatus
from app.control_plane.persistence.execution_models import ReviewExecutionRun
from app.infrastructure.observability.tracing import get_tracer, mark_span_ok, span_attributes


LEASE_SECONDS = 20
HEARTBEAT_SECONDS = 5


class ExecutionOwnershipError(RuntimeError):
    pass


class ActiveLeaseError(ExecutionOwnershipError):
    pass


class StaleExecutionOwnerError(ExecutionOwnershipError):
    pass


class CancelRequestedError(ExecutionOwnershipError):
    pass


class IncompatibleResumeError(ExecutionOwnershipError):
    pass


@dataclass(frozen=True)
class ExecutionLease:
    task_id: str
    attempt_id: str
    worker_id: str
    lease_epoch: int
    delivery_id: str
    lease_expires_at: datetime
    identity: dict[str, Any]
    node_id: str | None = None
    instance_id: str | None = None


current_execution_lease: ContextVar[ExecutionLease | None] = ContextVar(
    "current_review_execution_lease", default=None
)
current_execution_context: ContextVar[Any | None] = ContextVar(
    "current_review_execution_context", default=None
)


async def guard_managed_execution_write(db, task_id: str) -> bool:
    """Fence a managed write in the caller's transaction.

    Returns ``False`` only for explicitly legacy tasks without an execution row.
    """
    row = await db.get(ReviewExecutionRun, task_id)
    if row is None:
        return False
    context = current_execution_context.get()
    lease = current_execution_lease.get()
    context_identity = getattr(context, "identity", None)
    context_payload = (
        context_identity.model_dump(mode="json")
        if hasattr(context_identity, "model_dump")
        else context_identity
    )
    persisted_payload = dict(row.identity_json or {})
    persisted_digest = hashlib.sha256(
        json.dumps(persisted_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    context_digest = hashlib.sha256(
        json.dumps(context_payload or {}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        context is None
        or lease is None
        or not isinstance(context_payload, dict)
        or str(context_payload.get("task_id") or "") != task_id
        or context_digest != persisted_digest
        or context.attempt_id != lease.attempt_id
        or context.worker_id != lease.worker_id
        or context.lease_epoch != lease.lease_epoch
    ):
        raise StaleExecutionOwnerError("受管审查写入缺少或错配 ExecutionContext")
    await review_execution_ownership.assert_current_owner(db, lease)
    return True


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _database_now(db) -> datetime:
    bind = db.get_bind()
    clock = func.clock_timestamp() if bind.dialect.name == "postgresql" else func.now()
    return _aware(await db.scalar(select(clock)))


async def _locked_row(db, task_id: str) -> ReviewExecutionRun | None:
    result = await db.execute(
        select(ReviewExecutionRun)
        .where(ReviewExecutionRun.task_id == task_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


class ReviewExecutionOwnership:
    @get_tracer().start_as_current_span("ownership.claim")
    async def claim(
        self,
        db,
        task_id: str,
        *,
        worker_id: str,
        delivery_id: str,
        node_id: str | None = None,
        instance_id: str | None = None,
        lease_seconds: int = LEASE_SECONDS,
    ) -> ExecutionLease:
        claim_span = trace.get_current_span()
        claim_span.set_attributes(
            span_attributes(task_id=task_id, delivery_id=delivery_id, worker_id=worker_id)
        )
        row = await _locked_row(db, task_id)
        if row is None:
            await db.rollback()
            raise ExecutionOwnershipError("执行记录尚未初始化")
        task = await db.get(AgentTask, task_id)
        if task is None:
            await db.rollback()
            raise ExecutionOwnershipError("任务不存在")
        if task.status == AgentTaskStatus.COMPLETED:
            await db.rollback()
            raise ExecutionOwnershipError("已完成任务不得再次执行")
        now = await _database_now(db)
        if row.cancel_requested:
            await db.rollback()
            raise CancelRequestedError("任务已请求取消")
        if row.lease_expires_at is not None and _aware(row.lease_expires_at) > now:
            owner = row.worker_id
            epoch = row.lease_epoch
            await db.rollback()
            raise ActiveLeaseError(
                f"任务已有有效 owner: {owner} (epoch={epoch})"
            )
        row.lease_epoch = int(row.lease_epoch or 0) + 1
        row.attempt_id = str(uuid4())
        row.worker_id = worker_id
        row.node_id = node_id or worker_id
        row.instance_id = instance_id
        row.delivery_id = delivery_id
        row.last_heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        await db.commit()
        # claim 成功后 execution_attempt_id/lease_epoch 才是有效事实。
        claim_span.set_attribute("codesage.execution_attempt_id", str(row.attempt_id))
        claim_span.set_attribute("codesage.lease_epoch", int(row.lease_epoch))
        claim_span.set_attribute("codesage.status", "claimed")
        mark_span_ok(claim_span)
        return ExecutionLease(
            task_id=task_id,
            attempt_id=row.attempt_id,
            worker_id=worker_id,
            lease_epoch=row.lease_epoch,
            delivery_id=delivery_id,
            lease_expires_at=row.lease_expires_at,
            identity=dict(row.identity_json or {}),
            node_id=row.node_id,
            instance_id=row.instance_id,
        )

    async def renew(
        self,
        db,
        lease: ExecutionLease,
        *,
        lease_seconds: int = LEASE_SECONDS,
    ) -> datetime:
        now = await _database_now(db)
        expires = now + timedelta(seconds=lease_seconds)
        result = await db.execute(
            update(ReviewExecutionRun)
            .where(
                ReviewExecutionRun.task_id == lease.task_id,
                ReviewExecutionRun.worker_id == lease.worker_id,
                ReviewExecutionRun.instance_id == lease.instance_id,
                ReviewExecutionRun.lease_epoch == lease.lease_epoch,
                ReviewExecutionRun.cancel_requested.is_(False),
                ReviewExecutionRun.lease_expires_at >= now,
            )
            .values(last_heartbeat_at=now, lease_expires_at=expires)
        )
        if result.rowcount != 1:
            await db.rollback()
            row = await db.get(ReviewExecutionRun, lease.task_id)
            if row is not None and row.cancel_requested:
                await db.rollback()
                raise CancelRequestedError("任务已请求取消")
            await db.rollback()
            raise StaleExecutionOwnerError("lease 已过期、被接管或任务已取消")
        await db.commit()
        return expires

    async def assert_current_owner(self, db, lease: ExecutionLease) -> ReviewExecutionRun:
        row = await _locked_row(db, lease.task_id)
        now = await _database_now(db)
        if (
            row is None
            or row.worker_id != lease.worker_id
            or row.instance_id != lease.instance_id
            or row.lease_epoch != lease.lease_epoch
        ):
            await db.rollback()
            raise StaleExecutionOwnerError("旧 owner 的 lease_epoch 已失效")
        if row.cancel_requested:
            await db.rollback()
            raise CancelRequestedError("任务已请求取消，禁止提交")
        if row.lease_expires_at is None or _aware(row.lease_expires_at) < now:
            await db.rollback()
            raise StaleExecutionOwnerError("lease 已过期，禁止提交")
        return row

    async def request_cancel(self, db, task_id: str, *, commit: bool = True) -> bool:
        row = await _locked_row(db, task_id)
        if row is None:
            await db.rollback()
            return False
        row.cancel_requested = True
        if commit:
            await db.commit()
        else:
            await db.flush()
        return True

    async def prepare_resume(self, db, task_id: str, *, commit: bool = True) -> str:
        row = await _locked_row(db, task_id)
        if row is None:
            await db.rollback()
            raise IncompatibleResumeError(
                "历史任务缺少 ReviewRunIdentity，不能安全恢复；请创建新任务"
            )
        # 不改 identity_json/run_id。有效旧 lease 通过递增 epoch 立即作废。
        row.cancel_requested = False
        row.worker_id = None
        row.node_id = None
        row.instance_id = None
        row.attempt_id = None
        row.lease_epoch = int(row.lease_epoch or 0) + 1
        row.lease_expires_at = None
        row.last_heartbeat_at = None
        row.delivery_id = str(uuid4())
        if commit:
            await db.commit()
        else:
            await db.flush()
        return row.delivery_id

    async def release(self, db, lease: ExecutionLease) -> None:
        result = await db.execute(
            update(ReviewExecutionRun)
            .where(
                ReviewExecutionRun.task_id == lease.task_id,
                ReviewExecutionRun.worker_id == lease.worker_id,
                ReviewExecutionRun.instance_id == lease.instance_id,
                ReviewExecutionRun.lease_epoch == lease.lease_epoch,
            )
            .values(
                # node_id/instance_id remain as the last-attempt attribution;
                # worker_id/attempt_id/lease fields alone represent live ownership.
                worker_id=None, attempt_id=None, lease_expires_at=None,
            )
        )
        if result.rowcount != 1:
            await db.rollback()
            raise StaleExecutionOwnerError("结束 attempt 时所有权已变化")
        await db.commit()


review_execution_ownership = ReviewExecutionOwnership()
