"""TaskList tool tests."""

import pytest

import codesage.tools.builtin.interaction.task_list as task_list
from codesage.core.tasks import TaskStatus, TaskStore, TaskUpdate
from codesage.tools import ToolUseContext
from codesage.tools.builtin.interaction.task_list import TaskListTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    monkeypatch.setattr(task_list, "get_task_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_list_line_format(store, tmp_path):
    a = await store.create("default", subject="Fix auth", description="x")
    b = await store.create("default", subject="Write docs", description="y")
    await store.update("default", TaskUpdate(task_id=a.id, add_blocked_by=[b.id], owner="alice"))
    result = await TaskListTool().call({}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    lines = result.content.splitlines()
    assert lines[0] == "#1 [pending] Fix auth (alice) [blocked by #2]"
    assert lines[1] == "#2 [pending] Write docs"  # 无 owner/blocked 不标注


@pytest.mark.asyncio
async def test_list_empty(tmp_path):
    result = await TaskListTool().call({}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert result.content == "No tasks found"


@pytest.mark.asyncio
async def test_list_filters_completed_blockers(store, tmp_path):
    a = await store.create("default", subject="A", description="x")
    b = await store.create("default", subject="B", description="y")
    await store.update("default", TaskUpdate(task_id=b.id, add_blocked_by=[a.id]))
    await store.update("default", TaskUpdate(task_id=a.id, status=TaskStatus.COMPLETED))
    result = await TaskListTool().call({}, _ctx(tmp_path)).__anext__()
    lines = result.content.splitlines()
    assert lines[1] == "#2 [pending] B"  # 已完成 blocker 不标注


@pytest.mark.asyncio
async def test_list_ascending_by_id(store, tmp_path):
    await store.create("default", subject="First", description="x")
    await store.create("default", subject="Second", description="y")
    await store.create("default", subject="Third", description="z")
    result = await TaskListTool().call({}, _ctx(tmp_path)).__anext__()
    assert result.content.splitlines() == [
        "#1 [pending] First", "#2 [pending] Second", "#3 [pending] Third",
    ]


def test_needs_permissions_is_false():
    assert TaskListTool().needs_permissions({}) is False


def test_is_concurrency_safe():
    assert TaskListTool().is_concurrency_safe is True  # 只读
