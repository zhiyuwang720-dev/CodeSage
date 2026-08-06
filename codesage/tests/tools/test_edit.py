"""Edit tool tests: read-first requirement, stale-file guard, replacements."""

import os
import time

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.filesystem.edit import EditTool
from codesage.tools.builtin.filesystem.read import ReadTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


async def _read(ctx, path):
    await ReadTool().call({"file_path": path}, ctx).__anext__()


async def _edit(ctx, path, old, new, **extra):
    return await EditTool().call(
        {"file_path": path, "old_string": old, "new_string": new, **extra}, ctx
    ).__anext__()


@pytest.mark.asyncio
async def test_edit_requires_read_first(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one two three")
    result = await _edit(_ctx(tmp_path), "f.txt", "two", "2")
    assert result.is_error
    assert "Read the file first" in result.content


@pytest.mark.asyncio
async def test_edit_single_replacement(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one two three")
    ctx = _ctx(tmp_path)
    await _read(ctx, "f.txt")
    result = await _edit(ctx, "f.txt", "two", "2")
    assert not result.is_error
    assert f.read_text() == "one 2 three"


@pytest.mark.asyncio
async def test_edit_ambiguous_requires_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x y x")
    ctx = _ctx(tmp_path)
    await _read(ctx, "f.txt")
    result = await _edit(ctx, "f.txt", "x", "z")
    assert result.is_error
    assert "replace_all" in result.content


@pytest.mark.asyncio
async def test_edit_replace_all(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x y x")
    ctx = _ctx(tmp_path)
    await _read(ctx, "f.txt")
    result = await _edit(ctx, "f.txt", "x", "z", replace_all=True)
    assert not result.is_error
    assert f.read_text() == "z y z"


@pytest.mark.asyncio
async def test_edit_missing_old_string_errors(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("abc")
    ctx = _ctx(tmp_path)
    await _read(ctx, "f.txt")
    result = await _edit(ctx, "f.txt", "zzz", "q")
    assert result.is_error
    assert "not found" in result.content


@pytest.mark.asyncio
async def test_edit_after_own_edit_works(tmp_path):
    """A successful Edit refreshes the baseline; the next Edit needs no re-Read."""
    f = tmp_path / "f.txt"
    f.write_text("a b c")
    ctx = _ctx(tmp_path)
    await _read(ctx, "f.txt")
    assert not (await _edit(ctx, "f.txt", "b", "B")).is_error
    assert not (await _edit(ctx, "f.txt", "c", "C")).is_error
    assert f.read_text() == "a B C"


@pytest.mark.asyncio
async def test_edit_rejects_externally_changed_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one two")
    ctx = _ctx(tmp_path)
    await _read(ctx, "f.txt")
    f.write_text("one TWO")  # external change between Read and Edit
    result = await _edit(ctx, "f.txt", "two", "2")
    assert result.is_error
    assert "changed since Read" in result.content
    assert f.read_text() == "one TWO"  # untouched


@pytest.mark.asyncio
async def test_edit_allows_mtime_touch_only(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("one two")
    ctx = _ctx(tmp_path)
    await _read(ctx, "f.txt")
    os.utime(f, (time.time() + 5, time.time() + 5))  # touch: mtime moved, content same
    result = await _edit(ctx, "f.txt", "two", "2")
    assert not result.is_error
    assert f.read_text() == "one 2"


@pytest.mark.asyncio
async def test_edit_rejects_change_with_unchanged_mtime(tmp_path):
    """Same-mtime content change must still be caught (hash is the signal;
    an mtime match alone would miss writes that land on the same tick)."""
    f = tmp_path / "f.txt"
    f.write_text("one two")
    ctx = _ctx(tmp_path)
    await _read(ctx, "f.txt")
    recorded = f.stat().st_mtime_ns
    f.write_text("one TWO")
    os.utime(f, ns=(recorded, recorded))  # restore the recorded mtime exactly
    result = await _edit(ctx, "f.txt", "two", "2")
    assert result.is_error
    assert "changed since Read" in result.content
