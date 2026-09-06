from __future__ import annotations

import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.db.session import async_session_factory
from app.models.agent_task import AgentTask, AgentTaskStatus
from app.models.project import Project
from app.models.user import User
from app.services.contracts.review_execution import ReviewRunIdentity, sha256_bytes
from app.services.pr_review.execution_ownership import (
    ActiveLeaseError,
    CancelRequestedError,
    StaleExecutionOwnerError,
    review_execution_ownership,
)


pytestmark = pytest.mark.skipif(
    os.getenv("CODESAGE_WORKER_ACCEPTANCE") != "1",
    reason="仅由 tests/worker_acceptance/run-acceptance.ps1 启动真实依赖后运行",
)


def _identity(task_id: str) -> ReviewRunIdentity:
    return ReviewRunIdentity(
        run_id=str(uuid4()),
        task_id=task_id,
        source_kind="diff",
        repository_key="acceptance/fixture",
        diff_sha256=sha256_bytes(b"fixture diff"),
        config_fingerprint=sha256_bytes(b"fixture config"),
    )


@pytest.mark.asyncio
async def test_redis_is_real_and_reachable():
    redis = Redis.from_url(os.environ["REDIS_URL"])
    try:
        assert await redis.ping() is True
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_claim_cancel_resume_and_stale_epoch_on_postgres():
    suffix = uuid4().hex
    user_id = str(uuid4())
    project_id = str(uuid4())
    task_id = str(uuid4())
    async with async_session_factory() as db:
        db.add(User(id=user_id, email=f"acceptance-{suffix}@example.test", hashed_password="x"))
        db.add(Project(id=project_id, name=f"fixture-{suffix}", owner_id=user_id))
        db.add(
            AgentTask(
                id=task_id,
                project_id=project_id,
                created_by=user_id,
                version_label="acceptance",
                task_type="pr_review",
                status=AgentTaskStatus.PENDING,
            )
        )
        await db.commit()
        identity = _identity(task_id)
        await review_execution_ownership.initialize(db, identity, delivery_id="delivery-1")
        first = await review_execution_ownership.claim(
            db, task_id, worker_id="worker-a", delivery_id="delivery-1"
        )

    async with async_session_factory() as db:
        with pytest.raises(ActiveLeaseError):
            await review_execution_ownership.claim(
                db, task_id, worker_id="worker-b", delivery_id="duplicate"
            )
        assert await review_execution_ownership.request_cancel(db, task_id) is True

    async with async_session_factory() as db:
        with pytest.raises((CancelRequestedError, StaleExecutionOwnerError)):
            await review_execution_ownership.renew(db, first)
        delivery = await review_execution_ownership.prepare_resume(db, task_id)
        second = await review_execution_ownership.claim(
            db, task_id, worker_id="worker-b", delivery_id=delivery
        )
        assert second.identity.run_id == first.identity.run_id
        assert second.attempt_id != first.attempt_id
        assert second.lease_epoch > first.lease_epoch

    async with async_session_factory() as db:
        with pytest.raises(StaleExecutionOwnerError):
            await review_execution_ownership.assert_current_owner(db, first)

