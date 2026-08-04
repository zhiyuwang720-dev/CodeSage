"""Edit tool tests."""

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.filesystem.edit import EditTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.mark.asyncio
async def test_edit_single_replacement(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one two three")
    result = await EditTool().call(
        {"file_path": "f.txt", "old_string": "two", "new_string": "2"}, _ctx(tmp_path)
    ).__anext__()
    assert not result.is_error
    assert f.read_text() == "one 2 three"


@pytest.mark.asyncio
async def test_edit_ambiguous_requires_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x y x")
    result = await EditTool().call(
        {"file_path": "f.txt", "old_string": "x", "new_string": "z"}, _ctx(tmp_path)
    ).__anext__()
    assert result.is_error
    assert "replace_all" in result.content


@pytest.mark.asyncio
async def test_edit_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x y x")
    result = await EditTool().call(
        {"file_path": "f.txt", "old_string": "x", "new_string": "z", "replace_all": True}, _ctx(tmp_path)
    ).__anext__()
    assert not result.is_error
    assert f.read_text() == "z y z"


@pytest.mark.asyncio
async def test_edit_missing_old_string_errors(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("abc")
    result = await EditTool().call(
        {"file_path": "f.txt", "old_string": "zzz", "new_string": "q"}, _ctx(tmp_path)
    ).__anext__()
    assert result.is_error
    assert "not found" in result.content
