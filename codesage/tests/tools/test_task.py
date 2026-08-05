"""Background tasks: Bash run_in_background + TaskOutput/TaskStop + abort."""

import asyncio
import sys
import time

import pytest

from codesage.tools import ToolUseContext
from codesage.tools.builtin.shell.bash import BashTool
from codesage.tools.builtin.system.task import TaskOutputTool, TaskStopTool


def _ctx(tmp_path) -> ToolUseContext:
    return ToolUseContext(cwd=tmp_path)


def _bg_cmd(duration: int) -> str:
    if sys.platform == "win32":
        return f'"{sys.executable}" -c "import time; time.sleep({duration}); print(\'bg-done\')"'
    return f"sleep {duration} && echo bg-done"


async def _start_bg(ctx, duration=1) -> str:
    result = await BashTool().call({"command": _bg_cmd(duration), "run_in_background": True}, ctx).__anext__()
    assert not result.is_error
    task_id = result.metadata["task_id"]
    assert task_id
    return task_id


@pytest.mark.asyncio
async def test_background_start_returns_immediately(tmp_path):
    ctx = _ctx(tmp_path)
    t0 = time.monotonic()
    task_id = await _start_bg(ctx, duration=2)
    assert time.monotonic() - t0 < 1.0  # did not wait for the task


@pytest.mark.asyncio
async def test_task_output_blocks_until_done(tmp_path):
    ctx = _ctx(tmp_path)
    task_id = await _start_bg(ctx, duration=1)
    result = await TaskOutputTool().call({"task_id": task_id}, ctx).__anext__()
    assert not result.is_error
    assert "bg-done" in result.content
    assert "finished" in result.content


@pytest.mark.asyncio
async def test_task_output_unknown_id(tmp_path):
    result = await TaskOutputTool().call({"task_id": "does-not-exist"}, _ctx(tmp_path)).__anext__()
    assert result.is_error
    assert "unknown task_id" in result.content


@pytest.mark.asyncio
async def test_task_stop_kills_process_tree(tmp_path):
    ctx = _ctx(tmp_path)
    task_id = await _start_bg(ctx, duration=30)
    result = await TaskStopTool().call({"task_id": task_id}, ctx).__anext__()
    assert not result.is_error
    assert "Stopped" in result.content
    # stopped tasks are gone from the store
    result = await TaskOutputTool().call({"task_id": task_id}, ctx).__anext__()
    assert result.is_error
    assert "unknown task_id" in result.content


@pytest.mark.asyncio
async def test_task_stop_unknown_id(tmp_path):
    result = await TaskStopTool().call({"task_id": "nope"}, _ctx(tmp_path)).__anext__()
    assert result.is_error


@pytest.mark.asyncio
async def test_foreground_bash_unaffected_by_background(tmp_path):
    ctx = _ctx(tmp_path)
    await _start_bg(ctx, duration=1)
    result = await BashTool().call({"command": "echo fg-ok"}, ctx).__anext__()
    assert not result.is_error
    assert result.content.strip() == "fg-ok"


@pytest.mark.asyncio
async def test_abort_event_kills_foreground_bash(tmp_path):
    ctx = _ctx(tmp_path)
    event = asyncio.Event()
    ctx.abort_event = event
    cmd = "ping -n 60 127.0.0.1 > nul" if sys.platform == "win32" else "sleep 60"
    task = asyncio.create_task(BashTool().call({"command": cmd}, ctx).__anext__())
    await asyncio.sleep(0.5)
    event.set()
    result = await asyncio.wait_for(task, timeout=10)
    assert result.is_error
    assert "aborted" in result.content
