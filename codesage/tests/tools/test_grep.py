"""Grep tool tests."""

import shutil

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.search import grep as grep_module
from codesage.tools.builtin.search.grep import GrepTool

HAS_RG = shutil.which("rg") is not None


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


# --- rg fast path (real rg when available) ----------------------------------


@pytest.mark.skipif(not HAS_RG, reason="rg not installed")
@pytest.mark.asyncio
async def test_grep_uses_real_rg(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha\nbeta\nalpha again")
    result = await GrepTool().call({"pattern": "alpha"}, _ctx(tmp_path)).__anext__()
    assert "f.txt:1: alpha" in result.content
    assert "f.txt:3: alpha again" in result.content


@pytest.mark.skipif(not HAS_RG, reason="rg not installed")
@pytest.mark.asyncio
async def test_grep_rg_respects_i_glob_and_context(tmp_path):
    (tmp_path / "a.py").write_text("one\nHELLO\ntwo")
    (tmp_path / "b.md").write_text("hello nowhere\nx")
    result = await GrepTool().call(
        {"pattern": "hello", "-i": True, "glob": "*.py", "-C": 1}, _ctx(tmp_path)
    ).__anext__()
    assert "a.py:1: one" in result.content
    assert "a.py:2: HELLO" in result.content
    assert "a.py:3: two" in result.content
    assert "b.md" not in result.content


# --- rg unavailable -> pure-Python fallback ---------------------------------


@pytest.mark.asyncio
async def test_grep_falls_back_when_rg_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_module.shutil, "which", lambda _: None)
    f = tmp_path / "f.txt"
    f.write_text("hello world\nnothing")
    result = await GrepTool().call({"pattern": "hello"}, _ctx(tmp_path)).__anext__()
    assert "f.txt:1: hello world" in result.content


@pytest.mark.asyncio
async def test_grep_falls_back_when_rg_fails_to_run(tmp_path, monkeypatch):
    # rg "present" but the subprocess cannot start -> fallback, no crash
    monkeypatch.setattr(grep_module.shutil, "which", lambda _: "/nonexistent/rg")

    async def _fail(*args, **kwargs):
        return None, ""

    monkeypatch.setattr(grep_module, "_run_rg", _fail)
    (tmp_path / "f.txt").write_text("hi there")
    result = await GrepTool().call({"pattern": "hi"}, _ctx(tmp_path)).__anext__()
    assert "f.txt:1: hi there" in result.content


# --- context lines (-A/-B/-C) on the Python fallback ------------------------


@pytest.mark.asyncio
async def test_grep_context_after(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_module.shutil, "which", lambda _: None)
    (tmp_path / "f.txt").write_text("a\nb\nMATCH\nc\nd")
    result = await GrepTool().call({"pattern": "MATCH", "-A": 1}, _ctx(tmp_path)).__anext__()
    assert "f.txt:3: MATCH" in result.content
    assert "f.txt:4: c" in result.content  # after context
    assert "f.txt:2: b" not in result.content  # no before context
    assert "f.txt:5: d" not in result.content


@pytest.mark.asyncio
async def test_grep_context_both(tmp_path, monkeypatch):
    monkeypatch.setattr(grep_module.shutil, "which", lambda _: None)
    (tmp_path / "f.txt").write_text("a\nMATCH\nb\nMATCH\nc")
    result = await GrepTool().call({"pattern": "MATCH", "-C": 1}, _ctx(tmp_path)).__anext__()
    assert "f.txt:1: a" in result.content
    assert "f.txt:2: MATCH" in result.content
    assert "f.txt:3: b" in result.content
    assert "f.txt:4: MATCH" in result.content
    assert "f.txt:5: c" in result.content
