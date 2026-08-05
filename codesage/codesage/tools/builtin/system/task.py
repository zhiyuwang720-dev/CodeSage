"""Background tasks: registry + per-task output files, plus the
TaskOutput (read/block) and TaskStop (kill) tools that drive them.

BashTool starts tasks via BACKGROUND_STORE when run_in_background is set;
output is streamed to a temp-dir file so a task's lifetime is independent
of any single tool call.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO

from ...base import Tool, ToolResult, ToolUseContext
from ..shell.bash import kill_tree

POLL_INTERVAL_S = 0.2


class BackgroundTaskStore:
    """Registry of background shells; each task streams output to a file."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or Path(tempfile.mkdtemp(prefix="codesage-bg-"))
        self._tasks: dict[str, asyncio.subprocess.Process] = {}
        self._files: dict[str, BinaryIO] = {}

    def has(self, task_id: str) -> bool:
        return task_id in self._tasks

    async def start(self, command: str, *, cwd: Path, env: dict[str, str] | None = None) -> str:
        task_id = uuid.uuid4().hex[:12]
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        out_file = open(self._base_dir / f"{task_id}.out", "wb")
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                env=process_env,
                stdout=out_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=sys.platform != "win32",
            )
        except OSError:
            out_file.close()
            raise
        self._tasks[task_id] = proc
        self._files[task_id] = out_file
        return task_id

    async def is_done(self, task_id: str) -> bool | None:
        """True if finished, False if still running, None if unknown."""
        proc = self._tasks.get(task_id)
        if proc is None:
            return None
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=0)
            except asyncio.TimeoutError:
                pass
        return proc.returncode is not None

    async def read(self, task_id: str) -> str:
        proc = self._tasks.get(task_id)
        if proc is None:
            return f"Error: unknown task_id {task_id}"
        done = await self.is_done(task_id)
        out = ""
        f = self._files.get(task_id)
        if f is not None:
            try:
                out = Path(f.name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        if done:
            return out + f"\n[background {task_id}: finished, exit code {proc.returncode}]"
        return out + f"\n[background {task_id}: running]"

    async def stop(self, task_id: str) -> str:
        proc = self._tasks.get(task_id)
        if proc is None:
            return f"Error: unknown task_id {task_id}"
        if proc.returncode is None:
            kill_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        self._tasks.pop(task_id, None)
        f = self._files.pop(task_id, None)
        if f is not None:
            f.close()
        return f"Stopped {task_id}"


BACKGROUND_STORE = BackgroundTaskStore()


class TaskOutputTool(Tool):
    name = "TaskOutput"
    description = "Read the output of a background task started with run_in_background."
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "block": {"type": "boolean", "description": "Wait for completion (default true)"},
        },
        "required": ["task_id"],
    }
    is_concurrency_safe = True

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        task_id = str(input["task_id"])
        if not BACKGROUND_STORE.has(task_id):
            return ToolResult(await BACKGROUND_STORE.read(task_id), is_error=True)
        block = bool(input.get("block", True))
        while block and not await BACKGROUND_STORE.is_done(task_id):
            await asyncio.sleep(POLL_INTERVAL_S)
        return ToolResult(await BACKGROUND_STORE.read(task_id))


class TaskStopTool(Tool):
    name = "TaskStop"
    description = "Kill a background task and its process tree."
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    }
    is_concurrency_safe = False

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        out = await BACKGROUND_STORE.stop(str(input["task_id"]))
        return ToolResult(out, is_error=out.startswith("Error:"))
