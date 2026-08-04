"""Glob tool tests."""

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.search.glob import GlobTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.mark.asyncio
async def test_glob_finds_files_relative(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("")
    (tmp_path / "skip.md").write_text("")
    result = await GlobTool().call({"pattern": "**/*.py"}, _ctx(tmp_path)).__anext__()
    assert result.content == "a.py\nsub/b.py"


@pytest.mark.asyncio
async def test_glob_skips_ignored_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("")
    (tmp_path / "main.py").write_text("")
    result = await GlobTool().call({"pattern": "**/*"}, _ctx(tmp_path)).__anext__()
    assert "node_modules" not in result.content
    assert ".git" not in result.content
    assert "main.py" in result.content
