"""Write tool tests: create allowed, overwriting unread files requires Read."""

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.filesystem.read import ReadTool
from codesage.tools.builtin.filesystem.write import WriteTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.mark.asyncio
async def test_write_creates_directories(tmp_path):
    result = await WriteTool().call({"file_path": "a/b/c.txt", "content": "hello"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert (tmp_path / "a" / "b" / "c.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_write_existing_unread_file_errors(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("old content")
    result = await WriteTool().call({"file_path": "f.txt", "content": "new content"}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert "Read it first" in result.content
    assert f.read_text() == "old content"  # untouched


@pytest.mark.asyncio
async def test_write_after_read_overwrites(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("old content")
    ctx = _ctx(tmp_path)
    await ReadTool().call({"file_path": "f.txt"}, ctx).__anext__()
    result = await WriteTool().call({"file_path": "f.txt", "content": "new content"}, ctx).__anext__()
    assert not result.is_error
    assert f.read_text() == "new content"


@pytest.mark.asyncio
async def test_write_rejects_externally_changed_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one")
    ctx = _ctx(tmp_path)
    await ReadTool().call({"file_path": "f.txt"}, ctx).__anext__()
    f.write_text("TWO")  # external change
    result = await WriteTool().call({"file_path": "f.txt", "content": "three"}, ctx).__anext__()
    assert result.is_error
    assert "changed since Read" in result.content
