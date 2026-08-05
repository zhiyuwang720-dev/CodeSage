"""Bash tool: destructive-command guard, cd jail, real timeout, process-tree
kill, optional background execution with TaskOutput/TaskStop, abort channel.

The full 8-layer bash defense (LLM intent gate, sandbox plan, ...) lands in
phase 16. What ships here: static data-loss rules (Kode subset), agent_call
cd restriction to ctx.cwd, a hard timeout, and no orphaned process trees.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from ...base import Tool, ToolError, ToolResult, ToolUseContext

MAX_TIMEOUT_MS = 600_000
DEFAULT_TIMEOUT_MS = 120_000
OUTPUT_LIMIT_CHARS = 30_000

#: rm -rf targets that are always refused (data-loss rules, Kode subset).
_RM_RF_PROTECTED = ("/", "/home", "/root")
_RM_RF_PROTECTED_PATHS = {Path(p).resolve() for p in _RM_RF_PROTECTED}
#: `cd <path>` at command start or after a separator (quotes stripped).
_CD_RE = re.compile(r'(?:^|[;&|\r\n]\s*)cd(?:\s+(?:"([^"]*)"|\'([^\']*)\'|(\S+)))?')
#: Git Bash style "/e/Mac/..." → drive-letter path (models output POSIX-style
#: paths on Windows; Path() would resolve them against the *current* drive).
_GITBASH_PATH_RE = re.compile(r"^/([a-zA-Z])/(.*)$")


def _parse_target(target: str) -> Path:
    """Interpret a path argument the way the shell would on this platform.

    On Windows, Git Bash style `/e/Mac/...` means `E:\Mac\...` — Python's
    Path would attach the *current* drive instead, making in-cwd checks fail
    for perfectly legal commands.
    """
    t = target.strip("'\"")
    if sys.platform == "win32":
        m = _GITBASH_PATH_RE.match(t)
        if m:
            return Path(f"{m.group(1).upper()}:/{m.group(2)}")
    return Path(t)


def _rm_target_protected(target: str, cwd: Path) -> bool:
    """True when `rm -rf <target>` must be refused (empty, protected, or cwd)."""
    t = target.strip("'\"")
    if not t:
        return True  # rm -rf with no operand — refuse
    if t.rstrip("/") in _RM_RF_PROTECTED or t.rstrip("/") == "~":
        return True
    try:
        resolved = _parse_target(t).resolve()
        return resolved in _RM_RF_PROTECTED_PATHS or resolved == cwd.resolve()
    except OSError:
        return True


def check_destructive(command: str, cwd: Path) -> str | None:
    """Static guard: refuse data-destroying commands before they run."""
    tokens = command.split()
    for i, tok in enumerate(tokens):
        if tok == "rm" and i + 1 < len(tokens) and re.fullmatch(r"-(?:rf|fr)", tokens[i + 1]):
            target = tokens[i + 2] if i + 2 < len(tokens) else ""
            if _rm_target_protected(target, cwd):
                what = "on a protected path" if target else "with no target"
                return f"Command refused: rm -rf {what}"
        if tok.startswith("mkfs"):
            return "Command refused: mkfs* (filesystem creation) is not allowed"
        if tok == "shred":
            return "Command refused: shred is not allowed"
        if tok.startswith("of=/dev/") and tok != "of=/dev/null":
            return "Command refused: dd writing to a /dev/ device is not allowed"
        if (
            tok == "git"
            and i + 2 < len(tokens)
            and tokens[i + 1] == "reset"
            and tokens[i + 2] == "--hard"
        ):
            return "Command refused: git reset --hard is not allowed"
    return None


def check_cd(command: str, cwd: Path) -> str | None:
    """agent_call: every `cd` target must stay inside ctx.cwd."""
    base = cwd.resolve()
    for m in _CD_RE.finditer(command.lstrip()):
        target = m.group(1) or m.group(2) or m.group(3)
        if target is None or target == "-":  # bare `cd` (home) / `cd -` (old pwd)
            return "Command refused: cd outside the working directory"
        try:
            t = _parse_target(target).expanduser()
            if not t.is_absolute():
                t = cwd / t
            if not t.resolve().is_relative_to(base):
                return "Command refused: cd outside the working directory"
        except OSError:
            return "Command refused: cd outside the working directory"
    return None


class BashTool(Tool):
    name = "Bash"
    description = "Execute a shell command with a timeout; use for build/run/system tasks."
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_ms": {"type": "integer", "description": f"Timeout in ms (max {MAX_TIMEOUT_MS})"},
            "run_in_background": {"type": "boolean", "description": "Run in background and return a task_id (default false)"},
        },
        "required": ["command"],
    }
    is_concurrency_safe = False  # bash writes state; sequential barrier (phase 06)

    def needs_permissions(self, input: dict) -> bool:
        return True  # always gated (phase 05 consumes this)

    def validate_input(self, input: dict) -> None:
        timeout_ms = int(input.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        if timeout_ms < 0 or timeout_ms > MAX_TIMEOUT_MS:
            raise ToolError(f"timeout_ms must be in [0, {MAX_TIMEOUT_MS}], got {timeout_ms}")
        if not str(input.get("command") or "").strip():
            raise ToolError("command must not be empty")

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        command = str(input["command"])
        guard = check_destructive(command, ctx.cwd)
        if guard:
            return ToolResult(guard, is_error=True)
        if ctx.command_source == "agent_call":
            guard = check_cd(command, ctx.cwd)
            if guard:
                return ToolResult(guard, is_error=True)
        timeout_ms = int(input.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        timeout = timeout_ms / 1000
        if input.get("run_in_background"):
            from ..system.task import BACKGROUND_STORE  # lazy: breaks import cycle

            task_id = await BACKGROUND_STORE.start(command, cwd=ctx.cwd, env=ctx.env)
            return ToolResult(f"Background task started: {task_id}", metadata={"task_id": task_id})
        try:
            stdout, stderr, code = await run_shell(
                command, cwd=ctx.cwd, timeout=timeout, env=ctx.env, abort_event=ctx.abort_event
            )
        except ToolError as exc:
            return ToolResult(str(exc), is_error=True)

        out = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        if len(out) > OUTPUT_LIMIT_CHARS:
            out = out[:OUTPUT_LIMIT_CHARS] + "\n...(output truncated)"
        if code != 0:
            return ToolResult(f"Exit code {code}\n{out}", is_error=True)
        return ToolResult(out if out else "(no output)")


class _Aborted(Exception):
    """Internal: subprocess was aborted via ctx.abort_event."""


async def run_shell(
    command: str,
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None,
    abort_event: asyncio.Event | None = None,
) -> tuple[str, str, int]:
    """Run a command with a hard timeout; kills the whole process tree on
    expiry, on abort, or on caller cancellation."""
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    start_new_session = sys.platform != "win32"  # posix: own process group for killpg
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            env=process_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=start_new_session,
        )
    except OSError as exc:
        raise ToolError(f"Failed to start process: {exc}") from exc

    try:
        stdout_b, stderr_b = await _run_and_wait(proc, timeout, abort_event)
    except _Aborted:
        kill_tree(proc)
        await proc.wait()
        raise ToolError("aborted") from None
    except TimeoutError:
        kill_tree(proc)
        await proc.wait()
        raise ToolError(f"Command timed out after {timeout:.1f}s") from None
    except asyncio.CancelledError:
        kill_tree(proc)
        await proc.wait()
        raise
    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    return stdout, stderr, proc.returncode or 0


async def _run_and_wait(
    proc: asyncio.subprocess.Process, timeout: float, abort_event: asyncio.Event | None
) -> tuple[bytes, bytes]:
    """proc.communicate(), racing ctx.abort_event; raises TimeoutError/_Aborted."""
    comm = asyncio.ensure_future(proc.communicate())
    if abort_event is None:
        try:
            return await asyncio.wait_for(comm, timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError() from exc
    abort = asyncio.ensure_future(abort_event.wait())
    try:
        done, pending = await asyncio.wait({comm, abort}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        comm.cancel()
        abort.cancel()
        raise
    if abort in done:
        comm.cancel()
        await asyncio.gather(comm, return_exceptions=True)
        raise _Aborted() from None
    if not done:
        comm.cancel()
        abort.cancel()
        await asyncio.gather(comm, abort, return_exceptions=True)
        raise TimeoutError() from None
    abort.cancel()
    await asyncio.gather(abort, return_exceptions=True)
    return comm.result()


def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and its whole tree (taskkill /T on Windows, killpg on POSIX)."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()
