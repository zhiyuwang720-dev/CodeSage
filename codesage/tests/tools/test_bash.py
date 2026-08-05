"""Bash tool tests: real subprocess, timeout, exit codes."""

import sys

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.shell.bash import BashTool, run_shell


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
    await run_shell(f"sh -c '{child} & wait'", cwd=tmp_path, timeout=0.5, env=None)
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


@pytest.mark.asyncio
async def test_rm_rf_protected_refused(tmp_path):
    ctx = _ctx(tmp_path)
    for cmd in ["rm -rf /", "rm -rf ~", "rm -fr /home", f"rm -rf {tmp_path}"]:
        result = await BashTool().call({"command": cmd}, ctx).__anext__()
        assert result.is_error, cmd
        assert "refused" in result.content, cmd


@pytest.mark.asyncio
async def test_rm_rf_inside_cwd_allowed(tmp_path):
    (tmp_path / "sub").mkdir()
    result = await BashTool().call({"command": "rm -rf sub"}, _ctx(tmp_path)).__anext__()
    assert not result.is_error
    assert not (tmp_path / "sub").exists()


@pytest.mark.asyncio
async def test_data_loss_commands_refused(tmp_path):
    ctx = _ctx(tmp_path)
    for cmd in ["mkfs.ext4 /dev/sda", "mkfs /dev/sdb", "shred /etc/passwd", "dd if=/dev/zero of=/dev/sda"]:
        result = await BashTool().call({"command": cmd}, ctx).__anext__()
        assert result.is_error, cmd
        assert "refused" in result.content, cmd
    result = await BashTool().call({"command": "git reset --hard"}, ctx).__anext__()
    assert result.is_error and "refused" in result.content
    if sys.platform == "win32":
        return  # cmd.exe has no dd; the benign-device exemption is POSIX-covered
    # benign dd to /dev/null stays allowed
    result = await BashTool().call({"command": "dd if=/dev/zero of=/dev/null bs=4 count=1 2>/dev/null"}, ctx).__anext__()
    assert not result.is_error


@pytest.mark.asyncio
async def test_cd_outside_cwd_refused(tmp_path):
    ctx = _ctx(tmp_path)
    for cmd in ["cd /tmp && ls", "cd .. && ls", "cd ~", "cd -"]:
        result = await BashTool().call({"command": cmd}, ctx).__anext__()
        assert result.is_error, cmd
        assert "cd outside" in result.content, cmd


@pytest.mark.asyncio
async def test_cd_inside_cwd_allowed(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "marker.txt").write_text("x")
    ctx = _ctx(tmp_path)
    result = await BashTool().call({"command": "cd sub && ls marker.txt"}, ctx).__anext__()
    assert not result.is_error


@pytest.mark.asyncio
async def test_cd_restriction_skipped_in_user_bash_mode(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.command_source = "user_bash_mode"
    result = await BashTool().call({"command": "cd /tmp && pwd"}, ctx).__anext__()
    assert not result.is_error


def test_shell_argv_prefers_git_bash_on_windows(monkeypatch):
    """On Windows with Git Bash installed, commands run via `bash -c`
    (the model writes POSIX syntax; cmd.exe would break on `;` etc.)."""
    import codesage.tools.builtin.shell.bash as bash_mod

    monkeypatch.setattr(bash_mod.sys, "platform", "win32")
    monkeypatch.setattr(bash_mod.shutil, "which", lambda name: "C:/Program Files/Git/bin/bash.exe" if name == "bash" else None)
    argv, kind = bash_mod._shell_argv("git log; echo done")
    assert kind == "bash"
    assert argv == ["C:/Program Files/Git/bin/bash.exe", "-c", "git log; echo done"]


def test_shell_argv_falls_back_when_no_bash(monkeypatch):
    import codesage.tools.builtin.shell.bash as bash_mod

    monkeypatch.setattr(bash_mod.sys, "platform", "win32")
    monkeypatch.setattr(bash_mod.shutil, "which", lambda name: None)
    assert bash_mod._shell_argv("dir") is None


def test_shell_argv_posix_uses_default_shell(monkeypatch):
    import codesage.tools.builtin.shell.bash as bash_mod

    monkeypatch.setattr(bash_mod.sys, "platform", "linux")
    assert bash_mod._shell_argv("ls") is None
