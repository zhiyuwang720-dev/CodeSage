"""spec 09 P1: audit_stages store(register/start/complete/fail 幂等 upsert + 查询)。

覆盖:
- register 创建 pending 行且幂等(二次调用不重复)
- complete 写 findings 快照/session_id/stats, 未登记 stage 也能 upsert 建行
- start 置 running + 会话锚点; fail 置 failed + error_message
- completed() 只回 completed; get() 单行/None; touch_session 更新锚点
- AuditStage 契约字段验证; (task_id, stage_type) 唯一约束保证不重复
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.checkpoint import AuditStage, StageStatus
from app.infrastructure.persistence.stage_store import AuditStageStoreImpl

store = AuditStageStoreImpl()


@pytest.fixture
async def db_session(tmp_path):
    import app.models  # noqa: F401

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'p1_store.db'}", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_register_creates_pending_and_is_idempotent(db_session):
    task_id = "t-reg"
    planned = ["intake", "review:security", "report"]
    await store.register(db_session, task_id, planned)

    stages = await store.list(db_session, task_id)
    assert {s.stage_type for s in stages} == set(planned)
    assert all(s.status == StageStatus.pending for s in stages)
    assert all(s.stage_id == f"{task_id}:{s.stage_type}" for s in stages)

    # 幂等: 二次 register 不重复(唯一约束兜底)
    await store.register(db_session, task_id, planned)
    assert len(await store.list(db_session, task_id)) == len(planned)


async def test_complete_upserts_with_snapshot(db_session):
    task_id = "t-c"
    findings = [{"file_path": "a.py", "severity": "high"}]
    stage = await store.complete(
        db_session, task_id, "review:security",
        session_id="sess-1",
        stats={"turn_count": 5, "token_usage": 1200, "tool_calls": 8},
        findings=findings,
        payload={"perspective": "security"},
    )
    assert stage.status == StageStatus.completed
    assert stage.findings_count == 1
    assert stage.turn_count == 5
    assert stage.token_usage == 1200
    assert stage.session_id == "sess-1"
    assert stage.state_payload["findings"] == findings

    # 未登记 stage 也 upsert 建行(complete 即建)
    got = await store.get(db_session, task_id, "review:security")
    assert got is not None and got.state_payload["findings"] == findings


async def test_start_and_touch_session(db_session):
    task_id = "t-start"
    await store.register(db_session, task_id, ["review:quality"])
    started = await store.start(db_session, task_id, "review:quality", session_id="sess-q")
    assert started.status == StageStatus.running
    assert started.session_id == "sess-q"
    assert started.started_at is not None

    await store.touch_session(db_session, task_id, "review:quality", "sess-q2")
    got = await store.get(db_session, task_id, "review:quality")
    assert got.session_id == "sess-q2"


async def test_late_start_does_not_downgrade_completed_stage(db_session):
    task_id = "t-late-start"
    await store.complete(
        db_session, task_id, "review:security", session_id="sess-complete",
        findings=[], stats={"turn_count": 1},
    )

    stage = await store.start(
        db_session, task_id, "review:security", session_id="sess-late"
    )

    assert stage.status == StageStatus.completed
    assert stage.session_id == "sess-complete"


async def test_fail_sets_error(db_session):
    task_id = "t-f"
    await store.register(db_session, task_id, ["report"])
    failed = await store.fail(db_session, task_id, "report", "boom")
    assert failed.status == StageStatus.failed
    assert failed.error_message == "boom"
    assert failed.completed_at is not None


async def test_completed_only_returns_completed(db_session):
    task_id = "t-cmp"
    await store.register(db_session, task_id, ["intake", "report"])
    await store.complete(db_session, task_id, "intake", payload={})
    completed = await store.completed(db_session, task_id)
    assert [s.stage_type for s in completed] == ["intake"]
    # report 仍 pending, 不在 completed 列表(resume 会重跑它)
    assert await store.get(db_session, task_id, "report") is not None


async def test_get_missing_returns_none(db_session):
    assert await store.get(db_session, "t-none", "report") is None


def test_contract_validates_fields():
    AuditStage(stage_id="t:intake", task_id="t", stage_type="intake", status=StageStatus.pending)
    with pytest.raises(ValidationError):
        AuditStage(stage_id="t:intake", task_id="t", stage_type="intake", status="bogus")


def test_quick_stages_exclude_critic_and_deep_entries():
    """P4 预留: quick 模式 5 项, 不含 critic/anatomy/planning(落地时插入即可, 框架零改动)。"""
    from app.api.v1.endpoints.agent_tasks import QUICK_REVIEW_STAGES

    assert set(QUICK_REVIEW_STAGES) == {
        "intake", "review:security", "review:architecture", "review:quality", "report",
    }
    assert "critic" not in QUICK_REVIEW_STAGES
    assert "anatomy" not in QUICK_REVIEW_STAGES and "planning" not in QUICK_REVIEW_STAGES


def test_stage_type_accepts_reserved_deep_entries():
    """AuditStage.stage_type 是自由字符串: 深度审计的动态维度/critic 都可登记。"""
    for st in ("critic", "anatomy", "planning", "review:authz"):
        AuditStage(stage_id=f"t:{st}", task_id="t", stage_type=st, status=StageStatus.pending)


@pytest.mark.asyncio
async def test_sink_writes_review_stage_on_perspective_done(db_session, monkeypatch):
    """集成: sink 收到 session_start + perspective_done(findings 本体) → 写 review:* stage。"""
    from app.api.v1.endpoints.agent_tasks import _build_pr_review_event_sink
    from app.models.agent_task import AgentTask, AgentTaskStatus
    from app.models.review_execution import ReviewExecutionRun

    task = AgentTask(id="t-sink", project_id="p", version_label="v1", task_type="pr_review")
    task.status = AgentTaskStatus.RUNNING
    db_session.add(
        ReviewExecutionRun(
            task_id=task.id,
            identity_json={"run_id": "run-t-sink"},
            delivery_id="delivery-t-sink",
        )
    )
    await db_session.flush()

    async def allow_test_write(db, task_id):
        return True

    monkeypatch.setattr(
        "app.infrastructure.persistence.stage_store._guard_managed_write",
        allow_test_write,
    )

    class Em:
        def __init__(self):
            self.events = []

        async def add_event(self, *args, **kwargs):
            self.events.append(args)

    em = Em()
    sink = _build_pr_review_event_sink(task.id, em, task=task, db=db_session)

    await sink({"type": "meta", "repo": "o/r", "pr_number": 7})
    await sink({"type": "perspective_start", "perspective": "security"})
    await sink({"type": "session_start", "perspective": "security", "session_id": "sess-sec"})
    await sink(
        {
            "type": "done",
            "perspective": "security",
            "configured_model": "deepseek-chat",
            "request_model": "deepseek-chat",
            "response_model": "deepseek-chat-202609",
            "provider": "deepseek",
            "protocol": "openai_chat",
            "purpose": "review",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "field_sources": {"total_tokens": "provider"},
                "usage_present": True,
            },
        }
    )
    findings = [
        {
            "rule_id": "R1",
            "category": "security",
            "severity": "high",
            "title": "t",
            "description": "d",
            "file_path": "a.py",
            "line_start": 1,
            "line_end": 1,
            "confidence": 0.9,
            "needs_verification": False,
            "verdict": "confirmed",
            "source": "security",
        }
    ]
    await sink({"type": "perspective_done", "perspective": "security", "turn_count": 4, "findings": findings})

    stage = await store.get(db_session, task.id, "review:security")
    assert stage is not None
    assert stage.status == StageStatus.completed
    assert stage.session_id == "sess-sec"
    assert stage.turn_count == 4
    assert stage.findings_count == 1
    assert stage.state_payload["findings"] == findings
    assert stage.state_payload["perspective"] == "security"
    model_requests = stage.state_payload["stage_result"]["stats"]["model_requests"]
    assert model_requests == [
        {
            "configured_model": "deepseek-chat",
            "request_model": "deepseek-chat",
            "response_model": "deepseek-chat-202609",
            "provider": "deepseek",
            "endpoint_id": None,
            "protocol": "openai_chat",
            "perspective": "security",
            "purpose": "review",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "field_sources": {"total_tokens": "provider"},
                "usage_present": True,
            },
        }
    ]
