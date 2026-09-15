"""SQLAlchemy unit-of-work adapter for authoritative node result commits."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.contracts.platform import AttemptContext, ResultSubmission, SubmissionReceipt
from app.control_plane.scale_ops.ownership import ExecutionLease, review_execution_ownership


class SqlAlchemyResultCommitPort:
    def __init__(self, transaction, lease: ExecutionLease, submission: ResultSubmission):
        self._transaction = transaction
        self._lease = lease
        self._submission = submission

    async def commit(self, context: AttemptContext, business_write) -> SubmissionReceipt:
        if (
            context.task_id != self._submission.task_id
            or context.attempt_id != self._submission.attempt_id
            or context.lease_epoch != self._submission.epoch
            or context.task_id != self._lease.task_id
            or context.attempt_id != self._lease.attempt_id
            or context.lease_epoch != self._lease.lease_epoch
            or context.node_id != self._lease.worker_id
        ):
            raise ValueError("result submission does not match the claimed attempt")
        try:
            await review_execution_ownership.assert_current_owner(
                self._transaction, self._lease
            )
            await business_write(self._transaction)
            await self._transaction.commit()
        except BaseException:
            await self._transaction.rollback()
            raise
        return SubmissionReceipt(
            receipt_id=str(uuid4()),
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            epoch=context.lease_epoch,
            operation=self._submission.operation,
            accepted_at=datetime.now(timezone.utc),
            manifest_hash=self._submission.manifest_ref.sha256,
        )


def sqlalchemy_result_commit_port_factory(transaction, lease, submission):
    return SqlAlchemyResultCommitPort(transaction, lease, submission)

