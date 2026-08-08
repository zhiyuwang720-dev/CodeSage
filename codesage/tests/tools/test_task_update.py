"""TaskUpdate tool tests."""

import pytest

import codesage.tools.builtin.interaction.task_update as task_update
from codesage.core.tasks import TaskStatus, TaskStore, TaskUpdate
from codesage.tools import ToolError, ToolUseContext
from codesage.tools.builtin.interaction.task_update import TaskUpdateTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    monkeypatch.setattr(task_update, "get_task_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_update_fields(store, tmp_path):
    task = await store.create("default", subject="Fix auth", description="x")
    result = await TaskUpdateTool().call(
        {"taskId": task.id, "subject": "Fix auth v2", "owner": "alice",
         "metadata": {"prio": "high"}},
        _ctx(tmp_path),
    ).__anext__()
    assert not result.is_error
    assert result.content == "Updated task #1 (subject → Fix auth v2, owner → alice, metadata)"
    updated = store.get("default", task.id)
    assert updated.subject == "Fix auth v2"
    assert updated.owner == "alice"
    assert updated.metadata == {"prio": "high"}


@pytest.mark.asyncio
async def test_update_status_line(store, tmp_path):
    task = await store.create("default", subject="Fix auth", description="x")
    result = await TaskUpdateTool().call(
        {"taskId": task.id, "status": "in_progress"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert result.content == "Updated task #1 (status → in_progress)"
    assert store.get("default", task.id).status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_update_metadata_none_deletes_key(store, tmp_path):
    task = await store.create("default", subject="Fix auth", description="x",
                              metadata={"prio": "high", "keep": "yes"})
    result = await TaskUpdateTool().call(
        {"taskId": task.id, "metadata": {"prio": None}}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert result.content == "Updated task #1 (metadata)"
    assert store.get("default", task.id).metadata == {"keep": "yes"}  # null 删键


@pytest.mark.asyncio
async def test_update_no_change_reports_ok(store, tmp_path):
    task = await store.create("default", subject="Fix auth", description="x")
    result = await TaskUpdateTool().call(
        {"taskId": task.id}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert result.content == "Updated task #1 (ok)"


@pytest.mark.asyncio
async def test_update_completed_terminal_is_error(store, tmp_path):
    task = await store.create("default", subject="Fix auth", description="x")
    await store.update("default", TaskUpdate(task_id=task.id, status=TaskStatus.COMPLETED))
    result = await TaskUpdateTool().call(
        {"taskId": task.id, "status": "pending"}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert result.content == "Task #1 is completed"  # 终态拒绝回退


@pytest.mark.asyncio
async def test_update_deleted_removes_task(store, tmp_path):
    task = await store.create("default", subject="Fix auth", description="x")
    result = await TaskUpdateTool().call(
        {"taskId": task.id, "status": "deleted"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert result.content == "Deleted task #1"
    assert store.get("default", task.id) is None


def test_update_self_loop_rejected(tmp_path):
    # validate_input 先拒:addBlocks 与 addBlockedBy 同时含任务自身
    with pytest.raises(ToolError):
        TaskUpdateTool().validate_input(
            {"taskId": "1", "addBlocks": ["1"], "addBlockedBy": ["1"]})


@pytest.mark.asyncio
async def test_update_cycle_error_is_error(store, tmp_path):
    a = await store.create("default", subject="A", description="x")
    b = await store.create("default", subject="B", description="y")
    await store.update("default", TaskUpdate(task_id=a.id, add_blocked_by=[b.id]))  # b→a
    # b blocked by a → 与已有 b→a 成环 → 存储层拒绝,is_error 交模型自愈
    result = await TaskUpdateTool().call(
        {"taskId": b.id, "addBlockedBy": [a.id]}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert "cycle" in result.content


@pytest.mark.asyncio
async def test_update_missing_task_is_error(tmp_path):
    result = await TaskUpdateTool().call(
        {"taskId": "99", "status": "in_progress"}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert result.content == "Task not found: 99"


def test_update_empty_id_raises(tmp_path):
    with pytest.raises(ToolError):
        TaskUpdateTool().validate_input({"taskId": "  "})


def test_update_null_subject_raises(tmp_path):
    # 同 P3-2 防线:显式 None subject 拒绝
    with pytest.raises(ToolError):
        TaskUpdateTool().validate_input({"taskId": "1", "subject": None})


def test_needs_permissions_is_false():
    assert TaskUpdateTool().needs_permissions({}) is False


def test_is_concurrency_safe():
    assert TaskUpdateTool().is_concurrency_safe is False
