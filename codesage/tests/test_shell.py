"""Bash tool tests: real subprocess, timeout, exit codes."""

import sys

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.shell import BashTool, _run_shell


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


@pytest.mark.asyncio
async def test_bash_echo(tmp_path):
    result = await BashTool().call({"command": "echo hello"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert result.content.strip() == "hello"


@pytest.mark.asyncio
async def test_bash_nonzero_exit(tmp_path):
    result = await BashTool().call({"command": "exit 3"}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert "Exit code 3" in result.content


@pytest.mark.asyncio
async def test_bash_uses_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    result = await BashTool().call({"command": "ls marker.txt"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error


@pytest.mark.asyncio
async def test_timeout_kills_process_tree(tmp_path):
    if sys.platform == "win32":
        cmd = "ping -n 60 127.0.0.1 > nul"
    else:
        cmd = "sleep 60"
    result = await BashTool().call({"command": cmd, "timeout_ms": 1500}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert "timed out" in result.content.lower()


@pytest.mark.asyncio
async def test_timeout_kills_children(tmp_path):
    """A grandchild process must not outlive the timeout (process-tree kill)."""
    if sys.platform == "win32":
        pytest.skip("child-tree assertion is POSIX-oriented; Windows covered by taskkill /T in _kill_tree")
    # spawn a child that writes a marker after the parent would be killed
    child = "sh -c 'sleep 2 && echo done > marker.txt'"
    await _run_shell(f"sh -c '{child} & wait'", cwd=tmp_path, timeout=0.5, env=None)
    # give any leaked process a chance to write the marker; it must NOT appear
    import time

    time.sleep(2.5)
    assert not (tmp_path / "marker.txt").exists(), "grandchild survived the timeout kill"


@pytest.mark.asyncio
async def test_validate_timeout_range(tmp_path):
    from codesage.tools import ToolError

    tool = BashTool()
    with pytest.raises(ToolError):
        tool.validate_input({"command": "ls", "timeout_ms": 600_001})
