"""TodoWrite tool tests."""

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.interaction.todo import TodoWriteTool, reset_todos


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


async def _call(todos, tmp_path) -> str:
    result = await TodoWriteTool().call({"todos": todos}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    return result.content


@pytest.fixture(autouse=True)
def _clean_store():
    reset_todos()
    yield


@pytest.mark.asyncio
async def test_create_with_strings(tmp_path):
    out = await _call(["write tests", "fix bug"], tmp_path)
    assert out == "0/2 完成"


@pytest.mark.asyncio
async def test_update_replaces_list_idempotently(tmp_path):
    await _call(["a", "b", "c"], tmp_path)
    await _call(["a", "b"], tmp_path)  # full replace: c is dropped
    out = await _call(["a", "b"], tmp_path)  # same input -> same summary
    assert out == "0/2 完成"


@pytest.mark.asyncio
async def test_summary_counts_completed_and_in_progress(tmp_path):
    out = await _call(
        [
            {"content": "done", "status": "completed"},
            {"content": "doing", "status": "in_progress"},
            {"content": "later", "status": "pending"},
            {"content": "done2", "status": "completed"},
            {"content": "later2", "status": "pending"},
        ],
        tmp_path,
    )
    assert out == "2/5 完成 · 1 进行中: doing"


@pytest.mark.asyncio
async def test_passthrough_fields_preserved(tmp_path):
    await _call([{"content": "x", "status": "pending", "priority": "high", "tags": ["a"], "estimated_hours": 2}], tmp_path)
    from codesage.tools.builtin.interaction.todo import _STORE

    assert _STORE["default"][0]["priority"] == "high"
    assert _STORE["default"][0]["estimated_hours"] == 2


@pytest.mark.asyncio
async def test_multiple_in_progress_rejected(tmp_path):
    result = await TodoWriteTool().call(
        {"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]},
        _ctx(tmp_path),
    ).__anext__()
    assert result.is_error
    assert "Only one task can be in_progress" in result.content


@pytest.mark.asyncio
async def test_invalid_status_rejected(tmp_path):
    result = await TodoWriteTool().call(
        {"todos": [{"content": "a", "status": "banana"}]}, _ctx(tmp_path)
    ).__anext__()
    assert result.is_error
    assert "banana" in result.content


@pytest.mark.asyncio
async def test_empty_content_rejected(tmp_path):
    result = await TodoWriteTool().call({"todos": [{"content": "  "}]}, _ctx(tmp_path)).__anext__()
    assert result.is_error


def test_needs_permissions_is_false():
    assert TodoWriteTool().needs_permissions({}) is False


def test_validate_input_raises():
    from codesage.tools import ToolError

    tool = TodoWriteTool()
    with pytest.raises(ToolError):
        tool.validate_input({"todos": "not-a-list"})
    with pytest.raises(ToolError):
        tool.validate_input({"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]})
