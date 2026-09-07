from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.db.session import async_session_factory
from app.models.agent_task import AgentTask, AgentTaskStatus
from app.models.project import Project
from app.models.user import User
from app.api.v1.endpoints.agent_tasks import _save_findings
from app.services.contracts.review_execution import ExecutionContext, ReviewRunIdentity, sha256_bytes
from app.services.pr_review.execution import QuickReviewDependencies, execute_quick_review
from app.services.pr_review.execution_ownership import (
    StaleExecutionOwnerError,
    current_execution_context,
    current_execution_lease,
    guard_managed_execution_write,
    review_execution_ownership,
)
from app.services.session.stage_store import audit_stage_store


pytestmark = pytest.mark.skipif(
    os.getenv("CODESAGE_WORKER_ACCEPTANCE") != "1",
    reason="仅由一键验收启动真实依赖后运行",
)


async def _new_task(label: str) -> tuple[str, Path]:
    task_id = str(uuid4())
    user_id = str(uuid4())
    project_id = str(uuid4())
    root = Path(os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"])
    diff = root / "fixtures" / task_id / "review.diff"
    diff.parent.mkdir(parents=True, exist_ok=True)
    diff.write_text(f"diff --git a/a.py b/a.py\n+{label}\n", encoding="utf-8")
    async with async_session_factory() as db:
        db.add(User(id=user_id, email=f"{task_id}@example.test", hashed_password="x"))
        db.add(Project(id=project_id, name=label, owner_id=user_id))
        db.add(AgentTask(
            id=task_id,
            project_id=project_id,
            created_by=user_id,
            version_label="acceptance",
            task_type="pr_review",
            status=AgentTaskStatus.PENDING,
            audit_scope={"pr_review": {"diff_file_path": str(diff)}},
        ))
        await db.commit()
    return task_id, diff


def _identity(task_id: str, run_id: str | None = None) -> ReviewRunIdentity:
    return ReviewRunIdentity(
        run_id=run_id or str(uuid4()),
        task_id=task_id,
        source_kind="diff",
        repository_key="acceptance/supervision",
        diff_sha256=sha256_bytes(b"fixture"),
        config_fingerprint=sha256_bytes(b"config"),
    )


@pytest.mark.asyncio
async def test_runner_business_failed_is_not_reported_completed():
    task_id, _ = await _new_task("business-failed")

    async def runner(current_task_id: str) -> None:
        async with async_session_factory() as db:
            assert await guard_managed_execution_write(db, current_task_id) is True
            task = await db.get(AgentTask, current_task_id)
            task.status = AgentTaskStatus.FAILED
            task.error_message = "fixture business failure"
            await db.commit()

    result = await execute_quick_review(
        task_id,
        QuickReviewDependencies(
            runner=runner,
            worker_id="supervision-failed",
            artifact_root=os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"],
        ),
    )
    assert result == AgentTaskStatus.FAILED


@pytest.mark.asyncio
async def test_runner_without_terminal_status_is_rejected():
    task_id, _ = await _new_task("incomplete")

    async def runner(_: str) -> None:
        return None

    with pytest.raises(RuntimeError, match="without terminal business status"):
        await execute_quick_review(
            task_id,
            QuickReviewDependencies(
                runner=runner,
                worker_id="supervision-incomplete",
                artifact_root=os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"],
            ),
        )


class _FailingHeartbeatFactory:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == 2:
            return self._BrokenContext()
        return async_session_factory()

    class _BrokenContext:
        async def __aenter__(self):
            raise RuntimeError("heartbeat database unavailable")

        async def __aexit__(self, *args):
            return False


@pytest.mark.asyncio
async def test_heartbeat_database_error_cancels_runner_and_propagates():
    task_id, _ = await _new_task("heartbeat-failure")
    stopped = asyncio.Event()

    async def runner(_: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(RuntimeError, match="heartbeat database unavailable"):
        await execute_quick_review(
            task_id,
            QuickReviewDependencies(
                session_factory=_FailingHeartbeatFactory(),
                runner=runner,
                worker_id="supervision-heartbeat",
                artifact_root=os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"],
                heartbeat_interval=0.01,
            ),
        )
    assert stopped.is_set()
    async with async_session_factory() as db:
        task = await db.get(AgentTask, task_id)
        assert task.status != AgentTaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_cross_session_cancel_stops_runner_without_late_completion():
    task_id, _ = await _new_task("cancel")
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def runner(_: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    execution = asyncio.create_task(execute_quick_review(
        task_id,
        QuickReviewDependencies(
            runner=runner,
            worker_id="supervision-cancel",
            artifact_root=os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"],
            heartbeat_interval=0.01,
        ),
    ))
    await asyncio.wait_for(started.wait(), timeout=2)
    async with async_session_factory() as db:
        assert await review_execution_ownership.request_cancel(
            db, task_id, commit=False
        ) is True
        task = await db.get(AgentTask, task_id)
        task.status = AgentTaskStatus.CANCELLED
        await db.commit()
    assert await asyncio.wait_for(execution, timeout=2) == AgentTaskStatus.CANCELLED
    assert stopped.is_set()
    async with async_session_factory() as db:
        task = await db.get(AgentTask, task_id)
        assert task.status == AgentTaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_duplicate_first_delivery_keeps_one_stable_identity():
    task_id, _ = await _new_task("duplicate-initialize")
    candidates = [_identity(task_id), _identity(task_id)]

    async def initialize(candidate):
        async with async_session_factory() as db:
            row = await review_execution_ownership.initialize(
                db, candidate, delivery_id=str(uuid4())
            )
            return ReviewRunIdentity.model_validate(row.identity_json)

    first, second = await asyncio.gather(*(initialize(item) for item in candidates))
    assert first == second
    assert first.run_id in {item.run_id for item in candidates}


@pytest.mark.asyncio
async def test_old_owner_cannot_write_stage_or_findings_after_takeover():
    task_id, _ = await _new_task("old-owner")
    identity = _identity(task_id)
    async with async_session_factory() as db:
        await review_execution_ownership.initialize(db, identity, delivery_id="first")
        old_lease = await review_execution_ownership.claim(
            db, task_id, worker_id="old-worker", delivery_id="first"
        )
    async with async_session_factory() as db:
        delivery = await review_execution_ownership.prepare_resume(db, task_id)
        new_lease = await review_execution_ownership.claim(
            db, task_id, worker_id="new-worker", delivery_id=delivery
        )
    assert new_lease.lease_epoch > old_lease.lease_epoch

    root = Path(os.environ["CODESAGE_ACCEPTANCE_ARTIFACT_ROOT"]).resolve()
    context = ExecutionContext(
        identity=old_lease.identity,
        attempt_id=old_lease.attempt_id,
        worker_id=old_lease.worker_id,
        lease_epoch=old_lease.lease_epoch,
        workspace_root=str(root),
        artifact_root=str(root),
        deadline_at=datetime.now(timezone.utc),
    )
    lease_token = current_execution_lease.set(old_lease)
    context_token = current_execution_context.set(context)
    try:
        async with async_session_factory() as db:
            with pytest.raises(StaleExecutionOwnerError):
                await audit_stage_store.start(db, task_id, "review:security")
        async with async_session_factory() as db:
            with pytest.raises(StaleExecutionOwnerError):
                await _save_findings(db, task_id, [], commit=False)
    finally:
        current_execution_context.reset(context_token)
        current_execution_lease.reset(lease_token)

    async with async_session_factory() as db:
        await review_execution_ownership.assert_current_owner(db, new_lease)
        assert await audit_stage_store.get(db, task_id, "review:security") is None
