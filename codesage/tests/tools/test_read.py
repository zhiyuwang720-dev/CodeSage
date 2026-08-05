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


@pytest.mark.asyncio
async def test_read_output_capped_at_250kb(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 300_000)
    result = await ReadTool().call({"file_path": "big.txt"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert len(result.content) < 300_000
    assert "offset/limit" in result.content


@pytest.mark.asyncio
async def test_repeated_read_same_args_returns_stub(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello\nworld\n")
    ctx = _ctx(tmp_path)
    r1 = await ReadTool().call({"file_path": "f.txt"}, ctx).__anext__()
    assert "hello" in r1.content
    r2 = await ReadTool().call({"file_path": "f.txt"}, ctx).__anext__()
    assert "File unchanged since last Read" in r2.content
    assert not r2.is_error
    assert "hello" not in r2.content


@pytest.mark.asyncio
async def test_read_different_offset_not_stubbed(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("\n".join(f"line{i}" for i in range(10)))
    ctx = _ctx(tmp_path)
    r1 = await ReadTool().call({"file_path": "f.txt", "offset": 0, "limit": 3}, ctx).__anext__()
    assert "line0" in r1.content
    r2 = await ReadTool().call({"file_path": "f.txt", "offset": 5, "limit": 3}, ctx).__anext__()
    assert "line5" in r2.content
    assert "File unchanged" not in r2.content


@pytest.mark.asyncio
async def test_read_after_mtime_change_returns_full(tmp_path):
    import os

    f = tmp_path / "f.txt"
    f.write_text("old\n")
    ctx = _ctx(tmp_path)
    r1 = await ReadTool().call({"file_path": "f.txt"}, ctx).__anext__()
    assert "old" in r1.content
    f.write_text("new\n")
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000_000))
    r2 = await ReadTool().call({"file_path": "f.txt"}, ctx).__anext__()
    assert "new" in r2.content
    assert "File unchanged" not in r2.content
