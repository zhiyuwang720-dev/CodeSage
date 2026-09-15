"""Persistence adapter for the PR-owned ReviewRunIdentity."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.control_plane.persistence.execution_models import ReviewExecutionRun
from app.control_plane.scale_ops.ownership import IncompatibleResumeError
from app.nodes.pr_review.contracts.review_execution import ReviewRunIdentity


class IncompatibleReviewIdentityError(IncompatibleResumeError):
    pass


async def _locked_row(db, task_id: str) -> ReviewExecutionRun | None:
    result = await db.execute(
        select(ReviewExecutionRun).where(ReviewExecutionRun.task_id == task_id).with_for_update()
    )
    return result.scalar_one_or_none()


class ReviewExecutionIdentityStore:
    async def initialize(
        self,
        db,
        identity: ReviewRunIdentity,
        *,
        delivery_id: str | None = None,
        commit: bool = True,
    ):
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
            await (db.commit() if commit else db.flush())
            row = await db.get(ReviewExecutionRun, identity.task_id)
            if row is None:
                raise RuntimeError("执行身份初始化失败")
        persisted = ReviewRunIdentity.model_validate(row.identity_json)
        if persisted != identity.model_copy(update={"run_id": persisted.run_id}):
            await db.rollback()
            raise IncompatibleReviewIdentityError(
                "任务已有不同的运行身份；输入或配置已变化，请创建新任务"
            )
        await (db.commit() if commit else db.flush())
        return row

    async def load(self, db, task_id: str) -> ReviewRunIdentity | None:
        row = await db.get(ReviewExecutionRun, task_id)
        return ReviewRunIdentity.model_validate(row.identity_json) if row else None

    async def validate_resume(self, db, candidate: ReviewRunIdentity) -> ReviewRunIdentity:
        persisted = await self.load(db, candidate.task_id)
        if persisted is None:
            raise IncompatibleReviewIdentityError(
                "历史任务缺少 ReviewRunIdentity，不能安全复用旧 Checkpoint；请创建新任务"
            )
        if candidate.model_copy(update={"run_id": persisted.run_id}) != persisted:
            raise IncompatibleReviewIdentityError(
                "恢复身份不一致（diff、源码版本或配置指纹已变化）；请创建新任务"
            )
        return persisted


review_execution_identity_store = ReviewExecutionIdentityStore()
