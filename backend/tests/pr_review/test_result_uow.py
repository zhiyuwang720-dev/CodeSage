"""Real database transactions; SQLite does not prove PostgreSQL row locking."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db.base import Base
from app.models.agent_task import AgentFinding, AgentTask, AgentTaskStatus
from app.models.checkpoint import AuditStageORM
from app.models.project import Project
from app.models.user import User
from app.control_plane.results import ReviewResultService
from app.control_plane.execution_ownership import (
    CancelRequestedError, StaleExecutionOwnerError, current_execution_context,
    current_execution_lease, review_execution_ownership,
)
from app.contracts.final_review_contract import ReviewFinding
from app.contracts.review_execution import ExecutionContext, ReviewRunIdentity, sha256_bytes
from app.infrastructure.persistence.stage_store import audit_stage_store


@pytest.fixture
async def review_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'result.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(User(id="user", email="uow@example.test", hashed_password="x"))
        db.add(Project(id="project", name="uow", owner_id="user"))
        db.add(AgentTask(id="task", project_id="project", created_by="user",
                         version_label="test", task_type="pr_review", status=AgentTaskStatus.RUNNING))
        await db.commit()
        identity = ReviewRunIdentity(
            run_id="run", task_id="task", source_kind="diff", repository_key="uow/test",
            diff_sha256=sha256_bytes(b"diff"), config_fingerprint=sha256_bytes(b"config"),
        )
        await review_execution_ownership.initialize(db, identity, delivery_id="delivery")
        lease = await review_execution_ownership.claim(
            db, "task", worker_id="worker", delivery_id="delivery", lease_seconds=300,
        )
        for perspective in ("security", "architecture", "quality"):
            db.add(AuditStageORM(task_id="task", stage_id=f"task:review:{perspective}",
                                 stage_type=f"review:{perspective}", status="completed"))
        await db.commit()
    context = ExecutionContext(
        identity=identity, attempt_id=lease.attempt_id, worker_id=lease.worker_id,
        lease_epoch=lease.lease_epoch, workspace_root=str(tmp_path), artifact_root=str(tmp_path),
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    lease_token = current_execution_lease.set(lease)
    context_token = current_execution_context.set(context)
    try:
        yield factory, lease, tmp_path
    finally:
        current_execution_context.reset(context_token)
        current_execution_lease.reset(lease_token)
        await engine.dispose()


def finding():
    return ReviewFinding(
        rule_id="UOW", severity="medium", category="perf", title="UoW finding",
        description="specific evidence", file_path="a.py", line_start=1, line_end=1,
        confidence=0.9, needs_verification=False, verdict="confirmed", source="rules",
    )


async def assert_uncommitted(factory):
    async with factory() as db:
        assert (await db.get(AgentTask, "task")).status == AgentTaskStatus.RUNNING
        assert (await db.scalars(select(AgentFinding))).all() == []
        assert await audit_stage_store.get(db, "task", "report") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["fresh", "borrowed"])
async def test_uow_commits_stage_findings_and_terminal_together(review_db, entry):
    factory, lease, root = review_db
    service = ReviewResultService()
    if entry == "fresh":
        count = await service.accept_success(factory, "task", lease, [finding()], pr_meta={}, artifact_root=str(root))
    else:
        async with factory() as db:
            task = await db.get(AgentTask, "task")
            count = await service.commit_success(db, task, lease, [finding()], pr_meta={}, artifact_root=str(root))
    assert count == 1
    async with factory() as db:
        assert (await db.get(AgentTask, "task")).status == AgentTaskStatus.COMPLETED
        assert len((await db.scalars(select(AgentFinding))).all()) == 1
        stage = await audit_stage_store.get(db, "task", "report")
        assert stage.status.value == "completed"
        assert stage.state_payload["artifact_refs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stage", "finding", "commit"])
async def test_acceptance_failure_rolls_back_every_part(review_db, monkeypatch, failure):
    factory, lease, root = review_db
    service = ReviewResultService()
    if failure == "stage":
        original = audit_stage_store.complete

        async def fail_stage(*args, **kwargs):
            assert kwargs["commit"] is False
            await original(*args, **kwargs)
            raise ValueError("original stage error")

        monkeypatch.setattr(audit_stage_store, "complete", fail_stage)
    elif failure == "finding":
        def fail_finding(*args):
            raise ValueError("original finding error")

        monkeypatch.setattr(service, "_finding_row", fail_finding)
    else:
        original_factory = factory

        @asynccontextmanager
        async def failing_factory():
            async with original_factory() as db:
                async def fail_commit():
                    await db.flush()
                    raise ValueError("original commit error")
                monkeypatch.setattr(db, "commit", fail_commit)
                yield db

        factory = failing_factory
    with pytest.raises(ValueError, match=f"original {failure} error"):
        await service.accept_success(factory, "task", lease, [finding()], pr_meta={}, artifact_root=str(root))
    await assert_uncommitted(review_db[0])
    # Failure finalization uses a new session and preserves the original error.
    async with review_db[0]() as failure_db:
        await service.mark_failed(
            failure_db, "task", ValueError(f"original {failure} error"), lease=lease,
        )
    async with review_db[0]() as db:
        task = await db.get(AgentTask, "task")
        assert task.status == AgentTaskStatus.FAILED
        assert task.error_message == f"original {failure} error"
        assert await audit_stage_store.get(db, "task", "report") is None
        assert (await db.scalars(select(AgentFinding))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["stale", "cancelled"])
async def test_invalid_owner_cannot_commit(review_db, rejection):
    factory, lease, root = review_db
    async with factory() as db:
        if rejection == "stale":
            await review_execution_ownership.prepare_resume(db, "task")
        else:
            await review_execution_ownership.request_cancel(db, "task")
    error = StaleExecutionOwnerError if rejection == "stale" else CancelRequestedError
    with pytest.raises(error):
        await ReviewResultService().accept_success(factory, "task", lease, [finding()], pr_meta={}, artifact_root=str(root))
    await assert_uncommitted(factory)


@pytest.mark.asyncio
async def test_completion_gate_is_inside_acceptance_transaction(review_db):
    factory, lease, root = review_db
    async with factory() as db:
        stage = await db.scalar(select(AuditStageORM).where(AuditStageORM.stage_type == "review:quality"))
        stage.status = "running"
        await db.commit()
    with pytest.raises(RuntimeError, match="视角未完成"):
        await ReviewResultService().accept_success(factory, "task", lease, [], pr_meta={}, artifact_root=str(root))
    await assert_uncommitted(factory)


@pytest.mark.asyncio
async def test_late_failure_does_not_overwrite_completed_result(review_db):
    factory, lease, root = review_db
    service = ReviewResultService()
    await service.accept_success(factory, "task", lease, [], pr_meta={}, artifact_root=str(root))
    async with factory() as db:
        await service.mark_failed(db, "task", ValueError("late error"), lease=lease)
    async with factory() as db:
        task = await db.get(AgentTask, "task")
        assert task.status == AgentTaskStatus.COMPLETED
        assert task.error_message is None
