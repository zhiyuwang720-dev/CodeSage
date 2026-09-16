from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.bootstrap.result_uow import SqlAlchemyResultCommitPort
from app.contracts.platform import (
    AttemptContext,
    ResultManifestRef,
    ResultSubmission,
)
from app.control_plane.scale_ops.ownership import ExecutionLease


def _case():
    now = datetime.now(timezone.utc)
    task_id = str(uuid4())
    attempt_id = str(uuid4())
    delivery_id = str(uuid4())
    lease = ExecutionLease(
        task_id=task_id,
        attempt_id=attempt_id,
        worker_id="node-a",
        lease_epoch=3,
        delivery_id=delivery_id,
        lease_expires_at=now + timedelta(seconds=20),
        identity={"task_id": task_id},
        node_id="node-a",
        instance_id=attempt_id,
    )
    context = AttemptContext(
        task_id=task_id,
        attempt_id=attempt_id,
        node_id="node-a",
        instance_id=attempt_id,
        lease_epoch=3,
        delivery_id=delivery_id,
        lease_expires_at=lease.lease_expires_at,
        identity_digest="a" * 64,
    )
    submission = ResultSubmission(
        task_id=task_id,
        attempt_id=attempt_id,
        epoch=3,
        operation="final",
        idempotency_key="final-3",
        result_schema="pr_review.final.v1",
        manifest_ref=ResultManifestRef(
            artifact_id="final",
            sha256="b" * 64,
            size_bytes=10,
            media_type="application/json",
        ),
        outcome="completed",
    )
    return lease, context, submission


@pytest.mark.asyncio
async def test_22a_t05_uow_guards_business_write_and_commits_once(monkeypatch):
    lease, context, submission = _case()
    transaction = AsyncMock()
    guard = AsyncMock()
    monkeypatch.setattr(
        "app.bootstrap.result_uow.review_execution_ownership.assert_current_owner",
        guard,
    )
    write = AsyncMock()

    receipt = await SqlAlchemyResultCommitPort(
        transaction, lease, submission
    ).commit(context, write)

    guard.assert_awaited_once_with(transaction, lease)
    write.assert_awaited_once_with(transaction)
    transaction.commit.assert_awaited_once()
    transaction.rollback.assert_not_awaited()
    assert receipt.manifest_hash == submission.manifest_ref.sha256


@pytest.mark.asyncio
async def test_22a_t06_business_failure_rolls_back_without_commit(monkeypatch):
    lease, context, submission = _case()
    transaction = AsyncMock()
    monkeypatch.setattr(
        "app.bootstrap.result_uow.review_execution_ownership.assert_current_owner",
        AsyncMock(),
    )

    async def fail(_):
        raise RuntimeError("injected write failure")

    with pytest.raises(RuntimeError, match="injected write failure"):
        await SqlAlchemyResultCommitPort(transaction, lease, submission).commit(
            context, fail
        )
    transaction.rollback.assert_awaited_once()
    transaction.commit.assert_not_awaited()
