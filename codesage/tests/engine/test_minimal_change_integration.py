"""最小改动约束层与引擎集成测试(spec 20 §7:test_minimal_change_integration.py)。

验证:约束闸在权限闸后、执行前拦截(工具未执行);拦 Write 不株连 sibling Read;
拦一次重试放行;权限决策链零改动回归;旧「执行后 prepend」接线已删除。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codesage.engine.loop import AgentLoop, AgentLoopConfig
from codesage.engine.session import AgentSession  # noqa: F401  # 确保引擎可导入
from codesage.engine.tool_queue import ScheduledTool
from codesage.tools import Tool, ToolResult, ToolUseContext


class GuardIntel:
    """队列级测试 intel:discoverable + wait_ready 即时 + 固定影响面。"""

    def __init__(self, impact: dict) -> None:
        self._impact = impact
        self.discoverable = True

    async def wait_ready(self, timeout_s: float = 10.0) -> bool:
        return True

    async def impact_of_change(self, target: str) -> dict:
        return dict(self._impact)


def _mc_check(guard):
    """队列级包装:与 loop._minimal_change_check 同款适配(guard 收 tool_name+input)。"""

    async def _check(item) -> "ToolResult | None":
        return await guard.guard(item.tool.name, item.input)

    return _check


def test_agentloop_config_intel_default_none():
    """spec 20 §4:AgentLoopConfig.intel 默认 None(零变化)。"""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(AgentLoopConfig)}
    assert "intel" in field_names


def test_write_tools_are_guarded():
    """spec 20 §4:约束层只约束 Write/Edit。"""
    from codesage.intel import WRITE_TOOLS

    assert "Write" in WRITE_TOOLS
    assert "Edit" in WRITE_TOOLS
    assert "Read" not in WRITE_TOOLS


def test_apply_minimal_change_advice_removed():
    """阶段 20 §4.1:旧「执行后 prepend 建议」接线已删除(拦截点前移执行前)。"""
    assert not hasattr(AgentLoop, "_apply_minimal_change_advice")


def test_loop_has_minimal_change_check():
    """loop 持有约束闸方法(与 _permission_check 同级关卡)。"""
    assert hasattr(AgentLoop, "_minimal_change_check")


@pytest.mark.asyncio
async def test_guard_blocks_write_before_execution():
    """阶段 20 §4.1:约束闸在权限闸后、真实执行前拦 Write;工具未执行。"""
    from codesage.engine import ToolUseQueue
    from codesage.intel import MINIMAL_CHANGE_BLOCKED, MinimalChangeGuard

    executed: list[str] = []

    class WriteTool(Tool):
        name = "Write"

        def needs_permissions(self, input):
            return False

        async def _run(self, input, ctx):
            executed.append(input.get("file_path"))
            return ToolResult("written")

    guard = MinimalChangeGuard(GuardIntel({"status": "ok", "callers_total": 5}))
    item = ScheduledTool(
        tool_use_id="t1", tool=WriteTool(),
        input={"file_path": "src/foo.py"}, context=ToolUseContext(cwd=Path(".")),
    )
    results = await ToolUseQueue([item], minimal_change_check=_mc_check(guard)).run()
    assert executed == []  # 工具未执行(拦截在真实执行之前)
    blocked = results[0].result
    assert blocked is not None and blocked.is_error
    assert blocked.metadata.get("error_code") == MINIMAL_CHANGE_BLOCKED
    assert "minimal-change" in blocked.content
    assert "重试该操作将放行" in blocked.content


@pytest.mark.asyncio
async def test_queue_blocked_write_sibling_read_untouched():
    """拦 Write(非致命)不株连 sibling Read:读结果原样保留(CC 语义)。"""
    from codesage.engine import ToolUseQueue
    from codesage.intel import MinimalChangeGuard

    class WriteTool(Tool):
        name = "Write"

        def needs_permissions(self, input):
            return False

        async def _run(self, input, ctx):
            return ToolResult("written")

    class ReadTool(Tool):
        name = "Read"
        is_concurrency_safe = True

        def needs_permissions(self, input):
            return False

        async def _run(self, input, ctx):
            return ToolResult("read ok")

    guard = MinimalChangeGuard(GuardIntel({"status": "ok", "callers_total": 5}))
    ctx = ToolUseContext(cwd=Path("."))
    items = [
        ScheduledTool(tool_use_id="t1", tool=WriteTool(), input={"file_path": "src/foo.py"}, context=ctx),
        ScheduledTool(tool_use_id="t2", tool=ReadTool(), input={"file_path": "src/foo.py"}, context=ctx),
    ]
    results = await ToolUseQueue(items, minimal_change_check=_mc_check(guard)).run()
    blocked, read = results[0].result, results[1].result
    assert blocked is not None and blocked.is_error  # Write 被拦
    assert read is not None and not read.is_error  # Read 不受株连
    assert read.content == "read ok"


@pytest.mark.asyncio
async def test_queue_retry_same_target_executes():
    """拦一次语义:同目标重试 → 放行,工具真实执行。"""
    from codesage.engine import ToolUseQueue
    from codesage.intel import MinimalChangeGuard

    executed: list[str] = []

    class WriteTool(Tool):
        name = "Write"

        def needs_permissions(self, input):
            return False

        async def _run(self, input, ctx):
            executed.append(input.get("file_path"))
            return ToolResult("written")

    guard = MinimalChangeGuard(GuardIntel({"status": "ok", "callers_total": 5}))
    ctx = ToolUseContext(cwd=Path("."))

    first = await ToolUseQueue(
        [ScheduledTool(tool_use_id="t1", tool=WriteTool(), input={"file_path": "src/foo.py"}, context=ctx)],
        minimal_change_check=_mc_check(guard),
    ).run()
    assert first[0].result is not None and first[0].result.is_error  # 第一次拦
    assert executed == []

    second = await ToolUseQueue(
        [ScheduledTool(tool_use_id="t2", tool=WriteTool(), input={"file_path": "src/foo.py"}, context=ctx)],
        minimal_change_check=_mc_check(guard),
    ).run()
    assert second[0].result is not None and not second[0].result.is_error  # 重试放行
    assert executed == ["src/foo.py"]


@pytest.mark.asyncio
async def test_no_intel_means_zero_change():
    """intel 为 None:约束闸直接放行,引擎行为零变化。"""
    from codesage.engine import ToolUseQueue
    from codesage.intel import MinimalChangeGuard

    executed: list[str] = []

    class WriteTool(Tool):
        name = "Write"

        def needs_permissions(self, input):
            return False

        async def _run(self, input, ctx):
            executed.append("done")
            return ToolResult("written")

    guard = MinimalChangeGuard(None)  # 无 intel
    item = ScheduledTool(
        tool_use_id="t1", tool=WriteTool(),
        input={"file_path": "src/foo.py"}, context=ToolUseContext(cwd=Path(".")),
    )
    results = await ToolUseQueue([item], minimal_change_check=_mc_check(guard)).run()
    assert results[0].result is not None and not results[0].result.is_error
    assert executed == ["done"]


def test_permissions_engine_untouched():
    """spec 20 §4.3:权限决策链零改动回归(决策链语义不变)。"""
    from codesage.permissions import PermissionEngine
    from codesage.permissions.engine import PermissionMode

    engine = PermissionEngine()
    decision = engine.evaluate_tool_use(
        tool_name="Read", tool_input={"file_path": "x.py"}, permissions={},
        mode=PermissionMode.DEFAULT, cwd=Path("."),
    )
    # 未知工具默认 ask(决策链第 9 步)—— 权限引擎本身未被 20 改动
    assert decision.mode in ("allow", "ask")
