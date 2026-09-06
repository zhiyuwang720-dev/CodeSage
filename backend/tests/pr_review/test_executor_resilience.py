"""spec 09 P0 执行器正确性: 崩溃 → FAILED 转换 + 取消竞态门。

单元测试 `_execute_pr_review_task_impl` 的两个正确性前置:
1. 整链异常不再冒泡致任务卡 RUNNING: 统一落 FAILED + error_message。
2. 取消竞态门: 终态置 COMPLETED 前发现任务已取消 → 跳过覆写, 保持取消。

用临时文件 async SQLite(create_all 全量表)承接 audit_stages 落库 + fake pipeline
不触网。findings 走空列表路径(`_save_findings` 空输入直接 return 0), 绕开真实落库。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.agent_task import AgentTask, AgentTaskPhase, AgentTaskStatus
from app.services.agent.task_executor import (
    _cancelled_tasks,
    _running_asyncio_tasks,
    clear_task_cancellation,
    request_agent_task_cancellation,
)
from app.services.contracts.checkpoint import StageStatus
from app.services.session.stage_store import audit_stage_store
from app.api.v1.endpoints import agent_tasks as agent_tasks_mod


class StubEventManager:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def add_event(self, *args, **kwargs) -> None:
        self.events.append(args)


@pytest.fixture
async def db_session(tmp_path):
    """临时文件 async SQLite, 全量表建好(audit_stages 落库用)。"""
    import app.models  # noqa: F401  # 注册全部模型到 Base.metadata

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'p0_test.db'}", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _make_task(task_id: str = "task-p0") -> AgentTask:
    return AgentTask(
        id=task_id,
        project_id="proj-1",
        version_label="v1",
        task_type="pr_review",
        audit_scope={
            "pr_review": {
                "pr_url": "https://github.com/o/r/pull/1",
                "max_comments": 10,
            }
        },
        agent_config=None,
        status=AgentTaskStatus.PENDING,
        current_phase=AgentTaskPhase.PLANNING,
        max_iterations=50,
        total_files=0,
    )


def _make_project() -> SimpleNamespace:
    return SimpleNamespace(id="proj-1", name="r", repository_url="https://github.com/o/r")


def _patch_pipeline(
    monkeypatch,
    *,
    exc: Exception | None = None,
    comments=None,
    meta=None,
    emit_perspective_done: dict[str, list] | None = None,
):
    """把 command_router.run_review_pipeline_async 换成同步返回/抛错版本。

    注意 _execute_pr_review_task_impl 在函数体内 `from ... import run_review_pipeline_async`,
    每调用重新取模块属性, 因此 patch 模块属性即生效。

    emit_perspective_done: 视角→findings 映射; 非 None 时 fake pipeline 在返回前经
    event_sink 发 perspective_done(模拟 dispatcher 真实契约), 使 audit_stages 的
    review:* stage 落 completed —— 12-P2.2 完成 stage 闸门依赖该契约。
    """
    captured: dict = {}

    async def fake_pipeline(**kwargs):
        captured.update(kwargs)
        sink = kwargs.get("event_sink")
        if emit_perspective_done is not None and sink is not None:
            for perspective, findings in emit_perspective_done.items():
                await sink({
                    "type": "perspective_done",
                    "perspective": perspective,
                    "findings": findings,
                    "turn_count": 1,
                    "session_id": f"s-{perspective}",
                })
        if exc is not None:
            raise exc
        return SimpleNamespace(comments=comments or [], meta=meta or {})

    monkeypatch.setattr(
        "app.services.pr_review.command_router.run_review_pipeline_async", fake_pipeline
    )
    return captured


@pytest.mark.asyncio
async def test_executor_crash_sets_task_failed(monkeypatch, db_session):
    """崩溃异常不再冒泡: 任务落 FAILED + error_message, 不卡 RUNNING。"""
    _patch_pipeline(monkeypatch, exc=RuntimeError("boom"))
    task = _make_task()
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(db_session, task, _make_project(), em)

    assert task.status == AgentTaskStatus.FAILED
    assert task.error_message == "boom"
    assert task.completed_at is not None
    # intake 已 completed(崩溃发生在 intake 之后); 其余未完成 stage 被置 failed
    stages = {s.stage_type: s for s in await audit_stage_store.list(db_session, task.id)}
    assert stages["intake"].status == StageStatus.completed
    assert stages["report"].status == StageStatus.failed
    assert all(
        stages[f"review:{p}"].status == StageStatus.failed
        for p in ("security", "architecture", "quality")
    )


@pytest.mark.asyncio
async def test_executor_parse_failure_sets_task_failed(monkeypatch, db_session):
    """输入校验失败(既无 pr_url 又无 diff)同样走 FAILED 分支。"""
    _patch_pipeline(monkeypatch)
    task = _make_task()
    task.audit_scope = {"pr_review": {}}  # pr_url 取不到 → ValueError
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(
        db_session, task, SimpleNamespace(id="p", name="r", repository_url=None), em
    )

    assert task.status == AgentTaskStatus.FAILED
    assert "neither pr_url nor diff" in task.error_message


@pytest.mark.asyncio
async def test_executor_success_sets_completed(monkeypatch, db_session):
    """回归: 无取消时正常完成 → COMPLETED(空 findings 走 _save_findings 早退)。"""
    _patch_pipeline(
        monkeypatch, comments=[], meta={"head_sha": "abc"},
        emit_perspective_done={"security": [], "architecture": [], "quality": []},
    )
    task = _make_task()
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(db_session, task, _make_project(), em)

    assert task.status == AgentTaskStatus.COMPLETED
    assert task.completed_at is not None
    assert task.findings_count == 0
    # pr_meta 快照进 agent_config
    assert (task.agent_config or {}).get("pr_meta", {}).get("head_sha") == "abc"
    # intake/report stage 已 completed
    stages = {s.stage_type: s for s in await audit_stage_store.list(db_session, task.id)}
    assert stages["intake"].status == StageStatus.completed
    assert stages["report"].status == StageStatus.completed
    assert stages["intake"].state_payload.get("pr_url") == "https://github.com/o/r/pull/1"


@pytest.mark.asyncio
async def test_executor_cancel_race_keeps_cancelled(monkeypatch, db_session):
    """取消竞态门: 取消信号在终态前到达 → COMPLETED 不被写入, 保持取消语义。"""
    task_id = "task-p0-cancel"
    _patch_pipeline(monkeypatch, comments=[], meta={})
    task = _make_task(task_id=task_id)
    task.status = AgentTaskStatus.RUNNING
    em = StubEventManager()

    try:
        request_agent_task_cancellation(task_id)  # 模拟 cancel 端点 in-process 信号
        assert task_id in _cancelled_tasks

        await agent_tasks_mod._execute_pr_review_task_impl(db_session, task, _make_project(), em)

        # 取消后被跳过 COMPLETED 覆写: 状态停留 RUNNING(cancel 端点另一会话已置 CANCELLED),
        # completed_at 保持 None(不被晚到完成态写入)。
        assert task.status == AgentTaskStatus.RUNNING
        assert task.completed_at is None
        assert task.current_step == "Cancelled during execution"
        # 收尾 done 事件不应发出(终态已跳过)
        assert not any(ev[1] == "task_complete" for ev in em.events)
        # report stage 不应被标记 completed
        stages = {s.stage_type: s for s in await audit_stage_store.list(db_session, task.id)}
        assert stages["report"].status != StageStatus.completed
    finally:
        clear_task_cancellation(task_id)


def test_cancel_registration_inner_wraps_execution(monkeypatch):
    """_execute_agent_task_impl 登记 running asyncio task, 供取消端点真正打断在飞流水线。"""
    import asyncio

    observed: dict = {}

    async def fake_inner(task_id: str) -> None:
        # 外层登记应已把当前 asyncio task 写进 registry
        observed["registered"] = _running_asyncio_tasks.get(task_id)

    monkeypatch.setattr(agent_tasks_mod, "_execute_agent_task_impl_inner", fake_inner)

    async def runner():
        async def body():
            await agent_tasks_mod._execute_agent_task_impl("task-reg")

        await asyncio.create_task(body())

    asyncio.run(runner())

    # 执行期间已登记(即当前 task), finally 之后清理
    assert observed["registered"] is not None
    assert "task-reg" not in _running_asyncio_tasks


@pytest.mark.asyncio
async def test_executor_resume_prefills_completed_stages(monkeypatch, db_session):
    """resume 消费: 已完成视角从 audit_stages 快照预填(零 LLM), 未完成视角照常分发。"""
    captured = _patch_pipeline(
        monkeypatch, comments=[], meta={},
        emit_perspective_done={"quality": []},  # quality 未预填 → 新建会话跑完发 done
    )
    task = _make_task(task_id="task-resume")
    task.agent_config = {"resume_from_checkpoint": True}
    em = StubEventManager()

    # 预置 stage: intake + security/architecture 已完成(带 findings 快照), quality 未开始
    await audit_stage_store.register(db_session, task.id, agent_tasks_mod.QUICK_REVIEW_STAGES)
    await audit_stage_store.complete(
        db_session, task.id, "intake",
        payload={"pr_url": "https://github.com/o/r/pull/1", "changed_files": 1},
    )
    security_findings = [{"file_path": "a.py", "severity": "high", "title": "x", "confidence": 0.9}]
    arch_findings = [{"file_path": "b.py", "severity": "medium", "title": "y", "confidence": 0.7}]
    await audit_stage_store.complete(
        db_session, task.id, "review:security", findings=security_findings,
        payload={"perspective": "security"},
    )
    await audit_stage_store.complete(
        db_session, task.id, "review:architecture", findings=arch_findings,
        payload={"perspective": "architecture"},
    )

    await agent_tasks_mod._execute_pr_review_task_impl(db_session, task, _make_project(), em)

    assert task.status == AgentTaskStatus.COMPLETED
    opts = captured["options"] or {}
    prefill = opts.get("prefill_handoffs") or {}
    assert set(prefill) == {"security", "architecture"}
    assert [f["title"] for f in prefill["security"]["key_findings"]] == ["x"]
    assert [f["title"] for f in prefill["architecture"]["key_findings"]] == ["y"]
    assert prefill["security"]["context_data"]["resumed"] is True
    # 未完成视角无会话锚点 → 不进 resume_sessions, orchestrator 新建会话
    assert opts.get("resume_sessions") == {}
    # 终态清 resume 标志, 下次不再走恢复分支
    assert "resume_from_checkpoint" not in (task.agent_config or {})


@pytest.mark.asyncio
async def test_executor_resume_continues_pending_session(monkeypatch, db_session):
    """未完成视角带会话锚点 → resume_sessions 透传, dispatcher 侧 L3 续跑该会话。"""
    captured = _patch_pipeline(
        monkeypatch, comments=[], meta={},
        emit_perspective_done={"quality": []},  # running 会话续跑完发 done
    )
    task = _make_task(task_id="task-resume-sess")
    task.agent_config = {"resume_from_checkpoint": True}
    em = StubEventManager()

    await audit_stage_store.register(db_session, task.id, agent_tasks_mod.QUICK_REVIEW_STAGES)
    await audit_stage_store.complete(db_session, task.id, "intake", payload={})
    await audit_stage_store.complete(
        db_session, task.id, "review:security", findings=[], payload={}
    )
    # quality 未完成但已建会话(running 锚点, 进程死在对话中途)
    await audit_stage_store.start(db_session, task.id, "review:quality", session_id="sess-q")

    await agent_tasks_mod._execute_pr_review_task_impl(db_session, task, _make_project(), em)

    assert (captured["options"] or {}).get("resume_sessions") == {"quality": "sess-q"}
    # 完成的 security 仍预填(空 findings → 贡献 0 条), quality 不在预填内
    prefill = (captured["options"] or {}).get("prefill_handoffs") or {}
    assert set(prefill) == {"security"}


@pytest.mark.asyncio
async def test_executor_hard_cancel_finalizes_cancelled(monkeypatch, db_session):
    """spec 11-P5: 取消打断在飞 → 流水线被 hard-cancel(CancelledError)→ 任务落 CANCELLED
    而非卡 RUNNING(裸冒泡会让 resume 端点拒绝续跑)。

    收尾后**不重抛** CancelledError: 任务经 Starlette BackgroundTasks 从 ASGI 请求生命周期
    await, 重抛会冒泡成 "Exception in ASGI application" 崩掉整个 uvicorn(11 冒烟实测
    exit 127)。收尾完成即正常返回(取消端点已先 commit CANCELLED)。
    """
    import asyncio

    _patch_pipeline(monkeypatch, exc=asyncio.CancelledError("cancelled"))
    task = _make_task(task_id="task-p0-hardcancel")
    task.status = AgentTaskStatus.RUNNING
    em = StubEventManager()

    # 正常返回(不抛), 而非重抛 CancelledError 崩掉宿主
    await agent_tasks_mod._execute_pr_review_task_impl(
        db_session, task, _make_project(), em
    )

    # 优雅收尾: 状态落 CANCELLED, 而非当崩溃置 FAILED / 卡 RUNNING
    assert task.status == AgentTaskStatus.CANCELLED
    assert task.completed_at is not None
    assert task.current_step == "Cancelled during execution"
    # intake 已过(complete); 其余未完成 stage 置 failed(resume 对非 completed 续跑)
    stages = {s.stage_type: s for s in await audit_stage_store.list(db_session, task.id)}
    assert stages["intake"].status == StageStatus.completed
    assert stages["report"].status == StageStatus.failed
    assert all(
        stages[f"review:{p}"].status == StageStatus.failed
        for p in ("security", "architecture", "quality")
    )
    # 取消事件落盘(供事件流以 task_cancel 终止)
    assert any(ev[1] == "task_cancel" for ev in em.events)


@pytest.mark.asyncio
async def test_executor_incomplete_stages_block_completion(monkeypatch, db_session):
    """spec 12-P2.2 完成闸门: 视角未全部落 completed(resume 续跑兜底空结果场景)
    → 任务置 FAILED(可续), 不假 COMPLETED。"""
    # fake pipeline 返回但从不发 perspective_done → 三视角 stage 停留在 pending
    _patch_pipeline(monkeypatch, comments=[], meta={})
    task = _make_task(task_id="task-incomplete")
    task.status = AgentTaskStatus.RUNNING
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(db_session, task, _make_project(), em)

    assert task.status == AgentTaskStatus.FAILED
    assert task.completed_at is not None
    assert "视角未完成" in (task.error_message or "")
    assert all(p in (task.error_message or "") for p in ("security", "architecture", "quality"))
    stages = {s.stage_type: s for s in await audit_stage_store.list(db_session, task.id)}
    assert all(stages[f"review:{p}"].status != StageStatus.completed
               for p in ("security", "architecture", "quality"))


@pytest.mark.asyncio
async def test_executor_partial_perspective_done_blocks_completion(monkeypatch, db_session):
    """部分视角完成不假完成: 仅 security 落 done → 其余未完成 → FAILED(镜像用户
    暂停→继续后三视角未跑完却假完成的场景)。"""
    _patch_pipeline(
        monkeypatch, comments=[], meta={},
        emit_perspective_done={"security": [{"file_path": "a.py", "severity": "high",
                                             "title": "x", "confidence": 0.9}]},
    )
    task = _make_task(task_id="task-partial")
    task.status = AgentTaskStatus.RUNNING
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(db_session, task, _make_project(), em)

    assert task.status == AgentTaskStatus.FAILED
    assert "architecture" in (task.error_message or "")
    assert "quality" in (task.error_message or "")
    stages = {s.stage_type: s for s in await audit_stage_store.list(db_session, task.id)}
    assert stages["review:security"].status == StageStatus.completed
    assert stages["review:architecture"].status != StageStatus.completed


@pytest.mark.asyncio
async def test_executor_no_diff_bypasses_completion_gate(monkeypatch, db_session):
    """空 diff 早退(no_diff)无视角运行 → 闸门旁路, 仍 COMPLETED(空评论), 不误拦。"""
    _patch_pipeline(monkeypatch, comments=[], meta={"empty_reason": "no_diff"})
    task = _make_task(task_id="task-nodiff")
    task.status = AgentTaskStatus.RUNNING
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(db_session, task, _make_project(), em)

    assert task.status == AgentTaskStatus.COMPLETED
    assert task.completed_at is not None
