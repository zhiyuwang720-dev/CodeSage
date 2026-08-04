"""Read tool tests."""

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.filesystem.read import ReadTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.mark.asyncio
async def test_read_with_offsets_and_line_numbers(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("\n".join(f"line{i}" for i in range(10)))
    result = await ReadTool().call({"file_path": "f.txt", "offset": 2, "limit": 3}, _ctx(tmp_path)).__anext__()
    assert "3\tline2" in result.content
    assert "5\tline4" in result.content
    assert "(truncated" in result.content


@pytest.mark.asyncio
async def test_read_missing_file_errors(tmp_path):
    result = await ReadTool().call({"file_path": "missing.txt"}, _ctx(tmp_path)).__anext__()
    assert result.is_error


@pytest.mark.asyncio
async def test_read_binary_detected(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    result = await ReadTool().call({"file_path": "bin.dat"}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert "binary" in result.content
