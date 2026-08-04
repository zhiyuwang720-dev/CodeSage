"""Filesystem tool tests."""

from pathlib import Path

import pytest

from codesage.tools import ToolResult, ToolUseContext
from codesage.tools.fs import EditTool, LSTool, ReadTool, WriteTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


def _run(tool, input, ctx):
    """Consume a tool's async generator and return its ToolResult."""
    return tool.call(input, ctx).__anext__()


@pytest.mark.asyncio
async def test_ls_lists_sorted_with_dir_suffix(tmp_path):
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "adir").mkdir()
    result = await _run(LSTool(), {"path": "."}, _ctx(tmp_path))
    assert result.content == "adir/\nb.txt"


@pytest.mark.asyncio
async def test_ls_missing_dir_errors(tmp_path):
    result = await _run(LSTool(), {"path": "nope"}, _ctx(tmp_path))
    assert result.is_error


@pytest.mark.asyncio
async def test_read_with_offsets_and_line_numbers(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("\n".join(f"line{i}" for i in range(10)))
    result = await _run(ReadTool(), {"file_path": "f.txt", "offset": 2, "limit": 3}, _ctx(tmp_path))
    assert "3\tline2" in result.content
    assert "5\tline4" in result.content
    assert "(truncated" in result.content


@pytest.mark.asyncio
async def test_read_missing_file_errors(tmp_path):
    result = await _run(ReadTool(), {"file_path": "missing.txt"}, _ctx(tmp_path))
    assert result.is_error


@pytest.mark.asyncio
async def test_read_binary_detected(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    result = await _run(ReadTool(), {"file_path": "bin.dat"}, _ctx(tmp_path))
    assert result.is_error
    assert "binary" in result.content


@pytest.mark.asyncio
async def test_write_creates_directories(tmp_path):
    result = await _run(WriteTool(), {"file_path": "a/b/c.txt", "content": "hello"}, _ctx(tmp_path))
    assert not result.is_error
    assert (tmp_path / "a" / "b" / "c.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_edit_single_replacement(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one two three")
    result = await _run(
        EditTool(), {"file_path": "f.txt", "old_string": "two", "new_string": "2"}, _ctx(tmp_path)
    )
    assert not result.is_error
    assert f.read_text() == "one 2 three"


@pytest.mark.asyncio
async def test_edit_ambiguous_requires_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x y x")
    result = await _run(
        EditTool(), {"file_path": "f.txt", "old_string": "x", "new_string": "z"}, _ctx(tmp_path)
    )
    assert result.is_error
    assert "replace_all" in result.content


@pytest.mark.asyncio
async def test_edit_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x y x")
    result = await _run(
        EditTool(), {"file_path": "f.txt", "old_string": "x", "new_string": "z", "replace_all": True}, _ctx(tmp_path)
    )
    assert not result.is_error
    assert f.read_text() == "z y z"


@pytest.mark.asyncio
async def test_edit_missing_old_string_errors(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("abc")
    result = await _run(
        EditTool(), {"file_path": "f.txt", "old_string": "zzz", "new_string": "q"}, _ctx(tmp_path)
    )
    assert result.is_error
    assert "not found" in result.content
