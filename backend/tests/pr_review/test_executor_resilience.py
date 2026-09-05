"""spec 09 P0 执行器正确性: 崩溃 → FAILED 转换 + 取消竞态门。

单元测试 `_execute_pr_review_task_impl` 的两个正确性前置:
1. 整链异常不再冒泡致任务卡 RUNNING: 统一落 FAILED + error_message。
2. 取消竞态门: 终态置 COMPLETED 前发现任务已取消 → 跳过覆写, 保持取消。

用 fake db(仅 commit/rollback) + stub event_manager, 不触真库不触网。
findings 走空列表路径(`_save_findings` 空输入直接 return 0), 绕开真实落库。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.agent_task import AgentTask, AgentTaskPhase, AgentTaskStatus
from app.services.agent.task_executor import (
    _cancelled_tasks,
    clear_task_cancellation,
    request_agent_task_cancellation,
)
from app.api.v1.endpoints import agent_tasks as agent_tasks_mod


class FakeDB:
    """仅需 commit/rollback 两个异步方法; 不承接真实查询。"""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


class StubEventManager:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def add_event(self, *args, **kwargs) -> None:
        self.events.append(args)


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


def _patch_pipeline(monkeypatch, *, exc: Exception | None = None, comments=None, meta=None):
    """把 command_router.run_review_pipeline_async 换成同步返回/抛错版本。

    注意 _execute_pr_review_task_impl 在函数体内 `from ... import run_review_pipeline_async`,
    每调用重新取模块属性, 因此 patch 模块属性即生效。
    """
    captured: dict = {}

    async def fake_pipeline(**kwargs):
        captured.update(kwargs)
        if exc is not None:
            raise exc
        return SimpleNamespace(comments=comments or [], meta=meta or {})

    monkeypatch.setattr(
        "app.services.pr_review.command_router.run_review_pipeline_async", fake_pipeline
    )
    return captured


@pytest.mark.asyncio
async def test_executor_crash_sets_task_failed(monkeypatch):
    """崩溃异常不再冒泡: 任务落 FAILED + error_message, 不卡 RUNNING。"""
    _patch_pipeline(monkeypatch, exc=RuntimeError("boom"))
    task = _make_task()
    db = FakeDB()
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(db, task, _make_project(), em)

    assert task.status == AgentTaskStatus.FAILED
    assert task.error_message == "boom"
    assert task.completed_at is not None
    # 至少有一次 RUNNING 提交 + 一次 FAILED 提交
    assert db.commits >= 2


@pytest.mark.asyncio
async def test_executor_parse_failure_sets_task_failed(monkeypatch):
    """输入校验失败(既无 pr_url 又无 diff)同样走 FAILED 分支。"""
    _patch_pipeline(monkeypatch)
    task = _make_task()
    task.audit_scope = {"pr_review": {}}  # pr_url 取不到 → ValueError
    db = FakeDB()
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(
        db, task, SimpleNamespace(id="p", name="r", repository_url=None), em
    )

    assert task.status == AgentTaskStatus.FAILED
    assert "neither pr_url nor diff" in task.error_message


@pytest.mark.asyncio
async def test_executor_success_sets_completed(monkeypatch):
    """回归: 无取消时正常完成 → COMPLETED(空 findings 走 _save_findings 早退)。"""
    _patch_pipeline(monkeypatch, comments=[], meta={"head_sha": "abc"})
    task = _make_task()
    db = FakeDB()
    em = StubEventManager()

    await agent_tasks_mod._execute_pr_review_task_impl(db, task, _make_project(), em)

    assert task.status == AgentTaskStatus.COMPLETED
    assert task.completed_at is not None
    assert task.findings_count == 0
    # pr_meta 快照进 agent_config
    assert (task.agent_config or {}).get("pr_meta", {}).get("head_sha") == "abc"


@pytest.mark.asyncio
async def test_executor_cancel_race_keeps_cancelled(monkeypatch):
    """取消竞态门: 取消信号在终态前到达 → COMPLETED 不被写入, 保持取消语义。"""
    task_id = "task-p0-cancel"
    _patch_pipeline(monkeypatch, comments=[], meta={})
    task = _make_task(task_id=task_id)
    task.status = AgentTaskStatus.RUNNING
    db = FakeDB()
    em = StubEventManager()

    try:
        request_agent_task_cancellation(task_id)  # 模拟 cancel 端点 in-process 信号
        assert task_id in _cancelled_tasks

        await agent_tasks_mod._execute_pr_review_task_impl(db, task, _make_project(), em)

        # 取消后被跳过 COMPLETED 覆写: 状态停留 RUNNING(cancel 端点另一会话已置 CANCELLED),
        # completed_at 保持 None(不被晚到完成态写入)。
        assert task.status == AgentTaskStatus.RUNNING
        assert task.completed_at is None
        assert task.current_step == "Cancelled during execution"
        # 收尾 done 事件不应发出(终态已跳过)
        assert not any(ev[1] == "task_complete" for ev in em.events)
    finally:
        clear_task_cancellation(task_id)


def test_cancel_registration_inner_wraps_execution(monkeypatch):
    """_execute_agent_task_impl 登记 running asyncio task, 供取消端点真正打断在飞流水线。"""
    import asyncio

    from app.services.agent.task_executor import _running_asyncio_tasks

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
