"""Glob/Grep 工具在 ripgrep 缺失时的纯 Python 兜底测试。

背景: 本机未装 rg 时工具硬报错(agents 空转); 兜底用 os.walk + fnmatch/re 保持同语义。
06-P4 起工具为 RuntimeTool 直实现(GlobRuntimeTool/GrepRuntimeTool), 直接经 validate/execute 调用。
"""
import asyncio
import os
import shutil
import tempfile

from app.services.tooling.read import GlobRuntimeTool, GrepRuntimeTool
from app.services.tooling.runtime import ToolExecutionContext


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


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(session_id="session-1", turn_id="turn-1", tool_use_id="tool-use-1", tool_call_id="tool-call-1")


def _run(tool, raw_input: dict):
    return asyncio.run(tool.execute(tool.validate_input(raw_input), _context()))


def test_glob_fallback_lists_files_without_rg(monkeypatch):
    tmp = _make_workspace()
    _patch_rg_missing(monkeypatch)
    tool = GlobRuntimeTool(project_root=tmp)
    payload = _run(tool, {"path": ".", "recursive": True})
    assert payload.is_error is False, payload.content
    assert payload.output_payload["file_count"] >= 2, "递归列出 a.py + sub/b.txt"
    assert "b.txt" in payload.content


def test_grep_fallback_matches_lines_without_rg(monkeypatch):
    tmp = _make_workspace()
    _patch_rg_missing(monkeypatch)
    tool = GrepRuntimeTool(project_root=tmp)
    payload = _run(tool, {"pattern": "foo"})
    assert payload.is_error is False, payload.content
    assert payload.output_payload["matches"] >= 2, "a.py 与 sub/b.txt 都命中"
    assert "a.py" in payload.content


def test_grep_fallback_case_insensitive_and_file_pattern(monkeypatch):
    tmp = _make_workspace()
    _patch_rg_missing(monkeypatch)
    tool = GrepRuntimeTool(project_root=tmp)
    insensitive = _run(tool, {"pattern": "FOO", "case_sensitive": False})
    assert insensitive.output_payload["matches"] >= 2
    patterned = _run(tool, {"pattern": "foo", "is_regex": True, "glob": "*.txt"})
    assert patterned.is_error is False
    assert patterned.output_payload["matches"] == 1, "只匹配 *.txt"
    assert "b.txt" in patterned.content


def test_glob_grep_still_work_with_rg_present():
    """rg 在场时原路径不受影响(回归; 无 rg 环境自动跳过)。"""
    if shutil.which("rg") is None:
        return
    tmp = _make_workspace()
    tool = GlobRuntimeTool(project_root=tmp)
    payload = _run(tool, {"path": ".", "recursive": True})
    assert payload.is_error is False
    assert payload.output_payload["file_count"] >= 2
    grep_payload = _run(GrepRuntimeTool(project_root=tmp), {"pattern": "foo"})
    assert grep_payload.is_error is False
    assert grep_payload.output_payload["matches"] >= 2
