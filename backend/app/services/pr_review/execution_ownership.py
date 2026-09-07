"""PostgreSQL 行锁驱动的任务级执行所有权。"""
from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.agent_task import AgentTask, AgentTaskStatus
from app.models.review_execution import ReviewExecutionRun
from app.services.contracts.review_execution import ExecutionContext, ReviewRunIdentity


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
    identity: ReviewRunIdentity


current_execution_lease: ContextVar[ExecutionLease | None] = ContextVar(
    "current_review_execution_lease", default=None
)
current_execution_context: ContextVar[ExecutionContext | None] = ContextVar(
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
    persisted_run_id = str((row.identity_json or {}).get("run_id") or "")
    if (
        context is None
        or lease is None
        or context.identity.task_id != task_id
        or context.identity.run_id != persisted_run_id
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
    async def initialize(
        self,
        db,
        identity: ReviewRunIdentity,
        *,
        delivery_id: str | None = None,
        commit: bool = True,
    ) -> ReviewExecutionRun:
        row = await _locked_row(db, identity.task_id)
        if row is None:
            values = {
                "task_id": identity.task_id,
                "identity_json": identity.model_dump(mode="json"),
                "lease_epoch": 0,
                "cancel_requested": False,
                "delivery_id": delivery_id or str(uuid4()),
            }
            if db.get_bind().dialect.name == "postgresql":
                await db.execute(
                    pg_insert(ReviewExecutionRun)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["task_id"])
                )
            else:
                db.add(ReviewExecutionRun(**values))
            if commit:
                await db.commit()
            else:
                await db.flush()
            row = await db.get(ReviewExecutionRun, identity.task_id)
            if row is None:
                raise ExecutionOwnershipError("执行身份初始化失败")
        persisted = ReviewRunIdentity.model_validate(row.identity_json)
        comparable = identity.model_copy(update={"run_id": persisted.run_id})
        if persisted != comparable:
            await db.rollback()
            raise IncompatibleResumeError(
                "任务已有不同的运行身份；输入或配置已变化，请创建新任务"
            )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return row

    async def load_identity(self, db, task_id: str) -> ReviewRunIdentity | None:
        row = await db.get(ReviewExecutionRun, task_id)
        return ReviewRunIdentity.model_validate(row.identity_json) if row else None

    async def validate_resume_identity(
        self, db, candidate: ReviewRunIdentity
    ) -> ReviewRunIdentity:
        persisted = await self.load_identity(db, candidate.task_id)
        if persisted is None:
            raise IncompatibleResumeError(
                "历史任务缺少 ReviewRunIdentity，不能安全复用旧 Checkpoint；请创建新任务"
            )
        comparable = candidate.model_copy(update={"run_id": persisted.run_id})
        if comparable != persisted:
            raise IncompatibleResumeError(
                "恢复身份不一致（diff、源码版本或配置指纹已变化）；请创建新任务"
            )
        return persisted

    async def claim(
        self,
        db,
        task_id: str,
        *,
        worker_id: str,
        delivery_id: str,
        lease_seconds: int = LEASE_SECONDS,
    ) -> ExecutionLease:
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
        row.delivery_id = delivery_id
        row.last_heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        await db.commit()
        return ExecutionLease(
            task_id=task_id,
            attempt_id=row.attempt_id,
            worker_id=worker_id,
            lease_epoch=row.lease_epoch,
            delivery_id=delivery_id,
            lease_expires_at=row.lease_expires_at,
            identity=ReviewRunIdentity.model_validate(row.identity_json),
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
        if row is None or row.worker_id != lease.worker_id or row.lease_epoch != lease.lease_epoch:
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
                ReviewExecutionRun.lease_epoch == lease.lease_epoch,
            )
            .values(worker_id=None, attempt_id=None, lease_expires_at=None)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise StaleExecutionOwnerError("结束 attempt 时所有权已变化")
        await db.commit()


review_execution_ownership = ReviewExecutionOwnership()
