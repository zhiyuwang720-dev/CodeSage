"""Bash tool: minimal safety — real timeout, cross-platform process-tree kill.

The full 8-layer bash defense (destructive guard, LLM intent gate, sandbox
plan, ...) lands in phase 16. What ships here is the floor: validated input,
a hard timeout, and no orphaned process trees. permission gating is the
permission engine's job (phase 05), declared via needs_permissions.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from ...base import Tool, ToolError, ToolResult, ToolUseContext

MAX_TIMEOUT_MS = 600_000
DEFAULT_TIMEOUT_MS = 120_000
OUTPUT_LIMIT_CHARS = 30_000


class BashTool(Tool):
    name = "Bash"
    description = "Execute a shell command with a timeout; use for build/run/system tasks."
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_ms": {"type": "integer", "description": f"Timeout in ms (max {MAX_TIMEOUT_MS})"},
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
        timeout_ms = int(input.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        timeout = timeout_ms / 1000
        try:
            stdout, stderr, code = await run_shell(command, cwd=ctx.cwd, timeout=timeout, env=ctx.env)
        except ToolError as exc:
            return ToolResult(str(exc), is_error=True)

        out = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        if len(out) > OUTPUT_LIMIT_CHARS:
            out = out[:OUTPUT_LIMIT_CHARS] + "\n...(output truncated)"
        if code != 0:
            return ToolResult(f"Exit code {code}\n{out}", is_error=True)
        return ToolResult(out if out else "(no output)")


async def run_shell(
    command: str, *, cwd: Path, timeout: float, env: dict[str, str] | None
) -> tuple[str, str, int]:
    """Run a command with a hard timeout; kills the whole process tree on expiry."""
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
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        kill_tree(proc)
        await proc.wait()
        raise ToolError(f"Command timed out after {timeout:.1f}s")
    except asyncio.CancelledError:
        kill_tree(proc)
        await proc.wait()
        raise
    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    return stdout, stderr, proc.returncode or 0


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
