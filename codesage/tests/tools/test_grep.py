"""Grep tool tests."""

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.search.grep import GrepTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.mark.asyncio
async def test_grep_with_line_numbers(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello world\nnothing\nhello again")
    result = await GrepTool().call({"pattern": "hello"}, _ctx(tmp_path)).__anext__()
    assert "f.txt:1: hello world" in result.content
    assert "f.txt:3: hello again" in result.content
    assert "nothing" not in result.content


@pytest.mark.asyncio
async def test_grep_case_insensitive(tmp_path):
    (tmp_path / "f.txt").write_text("Hello\nworld")
    result = await GrepTool().call({"pattern": "hello", "-i": True}, _ctx(tmp_path)).__anext__()
    assert "Hello" in result.content


@pytest.mark.asyncio
async def test_grep_glob_filter(tmp_path):
    (tmp_path / "a.py").write_text("TODO here")
    (tmp_path / "b.md").write_text("TODO here")
    result = await GrepTool().call({"pattern": "TODO", "glob": "*.py"}, _ctx(tmp_path)).__anext__()
    assert "a.py" in result.content
    assert "b.md" not in result.content


@pytest.mark.asyncio
async def test_grep_invalid_regex(tmp_path):
    result = await GrepTool().call({"pattern": "([unclosed"}, _ctx(tmp_path)).__anext__()
    assert result.is_error


@pytest.mark.asyncio
async def test_grep_no_matches(tmp_path):
    (tmp_path / "f.txt").write_text("nothing here")
    result = await GrepTool().call({"pattern": "zzz"}, _ctx(tmp_path)).__anext__()
    assert "No matches" in result.content
