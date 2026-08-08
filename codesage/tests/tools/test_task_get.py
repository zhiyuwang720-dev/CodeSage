"""TaskGet tool tests."""

import json

import pytest

import codesage.tools.builtin.interaction.task_get as task_get
from codesage.core.tasks import TaskStore
from codesage.tools import ToolError, ToolUseContext
from codesage.tools.builtin.interaction.task_get import TaskGetTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    monkeypatch.setattr(task_get, "get_task_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_get_existing_returns_json(store, tmp_path):
    await store.create("default", subject="Fix auth", description="tokens")
    result = await TaskGetTool().call({"taskId": "1"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    data = json.loads(result.content)  # 单行 JSON,模型无损解析
    assert data["id"] == "1"
    assert data["subject"] == "Fix auth"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_get_missing_is_error(tmp_path):
    result = await TaskGetTool().call({"taskId": "99"}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert result.content == "Task not found: 99"


def test_get_empty_id_raises(tmp_path):
    # 引擎在校验路径调用 validate_input;直调断言先抛
    with pytest.raises(ToolError):
        TaskGetTool().validate_input({"taskId": " "})


def test_needs_permissions_is_false():
    assert TaskGetTool().needs_permissions({}) is False


def test_is_concurrency_safe():
    assert TaskGetTool().is_concurrency_safe is True  # 只读
