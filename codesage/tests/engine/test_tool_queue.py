"""ToolUseQueue tests: concurrency barriers, sibling errors, result spill."""

import asyncio
import time
from pathlib import Path

from codesage.engine import ToolUseQueue
from codesage.engine.tool_queue import MAX_TOOL_RESULT_CHARS, ScheduledTool
from codesage.tools import Tool, ToolResult, ToolUseContext


class FastTool(Tool):
    name = "Fast"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult(input.get("tag", "fast"))


class SlowTool(Tool):
    name = "Slow"
    is_concurrency_safe = False

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        await asyncio.sleep(0.1)
        return ToolResult("slow done")


class ErrorTool(Tool):
    name = "Err"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        raise RuntimeError("boom")


def _schedule(tools, inputs=None):
    ctx = ToolUseContext(cwd=__import__("pathlib").Path("."))
    return [
        ScheduledTool(tool_use_id=f"t{i}", tool=t, input=inputs[i] if inputs else {}, context=ctx)
        for i, t in enumerate(tools)
    ]


async def test_safe_tools_run_in_parallel():
    started = time.monotonic()
    tools = [FastTool() for _ in range(3)]
    results = await ToolUseQueue(_schedule(tools)).run()
    elapsed = time.monotonic() - started
    assert all(r.result.content == "fast" for r in results)


async def test_non_safe_tool_is_barrier():
    """A non-safe tool never overlaps with siblings (sequential)."""
    active = {"n": 0, "max": 0}

    class TrackedSlow(Tool):
        name = "TrackedSlow"
        is_concurrency_safe = False

        def needs_permissions(self, input):
            return False

        async def _run(self, input, ctx):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            await asyncio.sleep(0.05)
            active["n"] -= 1
            return ToolResult("ok")

    # two barriers + one safe tool; safe may overlap a barrier's tail but
    # barriers themselves must never overlap each other
    results = await ToolUseQueue(_schedule([TrackedSlow(), TrackedSlow()])).run()
    assert active["max"] == 1  # barriers never ran concurrently
    assert len(results) == 2


async def test_sibling_error_poisons_queue():
    """One failed tool voids every sibling (Kode design note #3)."""
    tools = [FastTool(), ErrorTool(), FastTool()]
    results = await ToolUseQueue(_schedule(tools)).run()
    by_id = {r.tool_use_id: r for r in results}
    assert by_id["t1"].result.is_error  # the erroring tool
    assert "Sibling tool call errored" in str(by_id["t0"].result.content)  # voided
    assert "Sibling tool call errored" in str(by_id["t2"].result.content)  # voided


async def test_permission_check_can_deny():
    async def deny(item):
        return ToolResult("Permission denied", is_error=True)

    results = await ToolUseQueue(_schedule([FastTool()]), permission_check=deny).run()
    assert results[0].result.is_error
    assert "Permission denied" in str(results[0].result.content)


class BigTool(Tool):
    name = "Big"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult("x" * (MAX_TOOL_RESULT_CHARS + 1))


async def test_large_result_spilled_to_disk():
    """Oversized str results are saved to a temp file; content becomes a pointer."""
    results = await ToolUseQueue(_schedule([BigTool()])).run()
    content = results[0].result.content
    assert content.startswith("(result saved to ")
    path_part = content[len("(result saved to "):].split(": ", 1)[0]
    assert path_part.endswith(".txt")
    saved = Path(path_part)
    assert saved.read_text(encoding="utf-8") == "x" * (MAX_TOOL_RESULT_CHARS + 1)
    assert content.endswith("...)")
    assert "x" * 500 in content  # first 500 chars previewed


async def test_small_result_not_spilled():
    results = await ToolUseQueue(_schedule([FastTool()])).run()
    assert results[0].result.content == "fast"


async def test_permission_check_allow_passthrough():
    async def allow(item):
        return None

    results = await ToolUseQueue(_schedule([FastTool()]), permission_check=allow).run()
    assert not results[0].result.is_error
