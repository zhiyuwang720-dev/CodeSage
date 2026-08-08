"""TaskCreate tool tests."""

import pytest

import codesage.tools.builtin.interaction.task_create as task_create
from codesage.core.tasks import TaskStore
from codesage.tools import ToolError, ToolUseContext
from codesage.tools.builtin.interaction.task_create import TaskCreateTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    monkeypatch.setattr(task_create, "get_task_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_create_success(store, tmp_path):
    result = await TaskCreateTool().call(
        {"subject": "Fix auth bug", "description": "Session tokens expire early"},
        _ctx(tmp_path),
    ).__anext__()
    assert not result.is_error
    assert result.content == "Created task #1: Fix auth bug"
    assert len(store.list("default")) == 1


@pytest.mark.asyncio
async def test_create_active_form_and_metadata(store, tmp_path):
    result = await TaskCreateTool().call(
        {"subject": "Fix auth", "description": "tokens", "activeForm": "Fixing auth",
         "metadata": {"prio": "high"}},
        _ctx(tmp_path),
    ).__anext__()
    assert not result.is_error
    task = store.get("default", "1")
    assert task.active_form == "Fixing auth"
    assert task.metadata == {"prio": "high"}


def test_create_empty_subject_raises(tmp_path):
    # 引擎在校验路径调用 validate_input;直调断言先抛
    with pytest.raises(ToolError):
        TaskCreateTool().validate_input({"subject": "  ", "description": "d"})


def test_create_null_subject_raises(tmp_path):
    # 显式 None 拒绝,防 str(None) → "None" 字符串入库(P3-2)
    with pytest.raises(ToolError):
        TaskCreateTool().validate_input({"subject": None, "description": "d"})


@pytest.mark.asyncio
async def test_create_store_error_is_error(store, tmp_path):
    # 绕过 validate_input 直调 _run:存储层自守(空白 description)仍以 is_error 返回
    result = await TaskCreateTool()._run(
        {"subject": "s", "description": "  "}, _ctx(tmp_path))
    assert result.is_error
    assert "required" in result.content


def test_needs_permissions_is_false():
    assert TaskCreateTool().needs_permissions({}) is False


def test_is_concurrency_safe():
    assert TaskCreateTool().is_concurrency_safe is False
