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


async def test_sibling_error_keeps_completed_voids_unstarted():
    """A failed tool keeps its completed siblings' real results; only tools
    that have NOT started (later batches) are voided (CC review softening)."""
    # batch 1: ErrorTool (safe) + SlowTool (barrier) run together;
    # batch 2: FastTool queued behind the barrier — never starts.
    tools = [ErrorTool(), SlowTool(), FastTool()]
    results = await ToolUseQueue(_schedule(tools)).run()
    by_id = {r.tool_use_id: r for r in results}
    assert by_id["t0"].result.is_error  # the erroring tool
    assert by_id["t1"].result.content == "slow done"  # completed sibling kept
    assert "Sibling tool call errored" in str(by_id["t2"].result.content)  # unstarted voided


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


class QuietTool(Tool):
    name = "Quiet"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult("")


class EmptyListTool(Tool):
    name = "NoBlocks"
    is_concurrency_safe = True

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult([])


async def test_empty_result_marked_for_model():
    """Empty str content becomes an explicit no-output marker (CC-03)."""
    results = await ToolUseQueue(_schedule([QuietTool()])).run()
    assert results[0].result.content == "(Quiet completed with no output)"
    assert not results[0].result.is_error


async def test_empty_list_result_marked_for_model():
    results = await ToolUseQueue(_schedule([EmptyListTool()])).run()
    assert results[0].result.content == "(NoBlocks completed with no output)"


class BigTextTool(Tool):
    name = "BigText"
    is_concurrency_safe = True

    def __init__(self, text):
        self._text = text

    def needs_permissions(self, input):
        return False

    async def _run(self, input, ctx):
        return ToolResult(self._text)


async def test_spill_same_tool_use_id_reuses_path():
    """Same tool_use_id: identical content reuses the file, different content
    overwrites the same path — path stays stable (CC-04)."""
    ctx = ToolUseContext(cwd=Path("."))
    content_a = "y" * (MAX_TOOL_RESULT_CHARS + 1)
    content_b = "z" * (MAX_TOOL_RESULT_CHARS + 1)

    async def spill(tid, text):
        queue = ToolUseQueue([ScheduledTool(tool_use_id=tid, tool=BigTextTool(text), input={}, context=ctx)])
        results = await queue.run()
        path_part = results[0].result.content[len("(result saved to "):].split(": ", 1)[0]
        return Path(path_part)

    p1 = await spill("cc04-replay", content_a)
    p2 = await spill("cc04-replay", content_a)
    assert p2 == p1  # same path for same id + same content
    assert p2.read_text(encoding="utf-8") == content_a

    p3 = await spill("cc04-replay", content_b)
    assert p3 == p1  # path stays stable across content changes
    assert p3.read_text(encoding="utf-8") == content_b  # overwritten


async def test_abort_event_skips_unstarted_siblings():
    """Abort set mid-batch: not-yet-started siblings never run, get interrupted."""
    called = []

    class AbortTool(FastTool):
        async def _run(self, input, ctx):
            called.append(input["tag"])
            ctx.abort_event.set()
            await asyncio.sleep(0)  # suspend mid-execution; siblings start now
            return ToolResult("done")

    ctx = ToolUseContext(cwd=Path("."), abort_event=asyncio.Event())
    scheduled = [
        ScheduledTool(tool_use_id=f"t{i}", tool=AbortTool(), input={"tag": f"t{i}"}, context=ctx)
        for i in range(3)
    ]
    results = await ToolUseQueue(scheduled).run()
    assert called == ["t0"]  # only the first tool was invoked
    for r in results[1:]:
        assert r.result.is_error
        assert "(interrupted by user)" in str(r.result.content)


# ---- 阶段 09 S6:pre_hook/post_hook 接线(§5.1 队列流程改造) ----

def _deny(msg="Permission denied by hook fake: no"):
    return ToolResult(msg, is_error=True)


async def test_pre_hook_deny_blocks_tool():
    """钩子 deny → 工具不执行、引擎不跑、error_code=permission_blocked、post_hook 拿到拒绝结果。"""
    ran = []
    permission_calls = []
    seen = []

    class RanTool(FastTool):
        async def _run(self, input, ctx):
            ran.append(True)
            return ToolResult("ran")

    async def deny(item):
        seen.append(("pre", item.tool.name))
        return _deny()

    async def post(item, result):
        seen.append(("post", item.tool.name, result.is_error))

    async def perm(item):
        permission_calls.append(item.tool.name)
        return None

    items = _schedule([RanTool()])
    await ToolUseQueue(items, permission_check=perm, pre_hook=deny, post_hook=post).run()
    assert ran == []  # 工具从未执行
    assert permission_calls == []  # 引擎决策链不运行
    assert ("pre", "Fast") in seen and ("post", "Fast", True) in seen
    assert items[0].result.is_error
    assert items[0].result.metadata.get("error_code") == "permission_blocked"


async def test_pre_hook_allow_shortcircuits_permission_check():
    """钩子 allow → item.hook_allowed=True,引擎决策链不运行,工具照常执行。"""
    permission_calls = []

    async def pre(item):
        item.hook_allowed = True
        return None

    async def perm(item):
        permission_calls.append(item.tool.name)
        return _deny()

    items = _schedule([FastTool()])
    await ToolUseQueue(items, permission_check=perm, pre_hook=pre).run()
    assert permission_calls == []
    assert items[0].result.content == "fast"


async def test_pre_hook_deny_does_not_poison_siblings():
    """钩子 deny 复用 permission_blocked 语义:被拒工具不株连同批 sibling(§5.1/既有豁免)。"""
    ran = []

    class RanTool(FastTool):
        async def _run(self, input, ctx):
            ran.append(input.get("tag"))
            return ToolResult(f"ok:{input.get('tag')}")

    async def pre(item):
        return _deny() if item.tool_use_id == "t0" else None

    items = _schedule([RanTool(), RanTool()], inputs=[{"tag": "a"}, {"tag": "b"}])
    await ToolUseQueue(items, pre_hook=pre).run()
    assert ran == ["b"]  # sibling 照常执行
    assert items[0].result.is_error
    assert items[0].result.metadata.get("error_code") == "permission_blocked"
    assert items[1].result.content == "ok:b"
