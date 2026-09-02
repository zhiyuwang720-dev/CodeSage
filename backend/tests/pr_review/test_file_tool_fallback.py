"""Glob/Grep 工具在 ripgrep 缺失时的纯 Python 兜底测试。

背景: 本机未装 rg 时工具硬报错(agents 空转); 兜底用 os.walk + fnmatch/re 保持同语义。
"""
import asyncio
import os
import shutil
import tempfile

from app.services.agent.tools.file_tool import FileSearchTool, ListFilesTool


def _make_workspace() -> str:
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "a.py"), "w", encoding="utf-8") as fh:
        fh.write("def foo():\n    return 1\n")
    os.makedirs(os.path.join(tmp, "sub"), exist_ok=True)
    with open(os.path.join(tmp, "sub", "b.txt"), "w", encoding="utf-8") as fh:
        fh.write("hello foo world\n")
    return tmp


def _patch_rg_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)


def test_glob_fallback_lists_files_without_rg(monkeypatch):
    tmp = _make_workspace()
    _patch_rg_missing(monkeypatch)
    tool = ListFilesTool(project_root=tmp)
    result = asyncio.run(tool._execute(directory=".", recursive=True))
    assert result.success, result.error
    assert result.metadata["file_count"] >= 2, "递归列出 a.py + sub/b.txt"
    assert "b.txt" in result.data


def test_grep_fallback_matches_lines_without_rg(monkeypatch):
    tmp = _make_workspace()
    _patch_rg_missing(monkeypatch)
    tool = FileSearchTool(project_root=tmp)
    result = asyncio.run(tool._execute(keyword="foo"))
    assert result.success, result.error
    assert result.metadata["matches"] >= 2, "a.py 与 sub/b.txt 都命中"
    assert "a.py" in result.data


def test_grep_fallback_case_insensitive_and_file_pattern(monkeypatch):
    tmp = _make_workspace()
    _patch_rg_missing(monkeypatch)
    tool = FileSearchTool(project_root=tmp)
    insensitive = asyncio.run(tool._execute(keyword="FOO", case_sensitive=False))
    assert insensitive.metadata["matches"] >= 2
    patterned = asyncio.run(tool._execute(keyword="foo", is_regex=True, file_pattern="*.txt"))
    assert patterned.success
    assert patterned.metadata["matches"] == 1, "只匹配 *.txt"
    assert "b.txt" in patterned.data


def test_glob_grep_still_work_with_rg_present():
    """rg 在场时原路径不受影响(回归; 无 rg 环境自动跳过)。"""
    if shutil.which("rg") is None:
        return
    tmp = _make_workspace()
    tool = ListFilesTool(project_root=tmp)
    result = asyncio.run(tool._execute(directory=".", recursive=True))
    assert result.success
    assert result.metadata["file_count"] >= 2
    grep = asyncio.run(FileSearchTool(project_root=tmp)._execute(keyword="foo"))
    assert grep.success
    assert grep.metadata["matches"] >= 2
