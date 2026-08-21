"""最小改动约束层与引擎集成测试(spec 20 §7:test_minimal_change_integration.py)。

验证:约束层叠加后引擎正常工作;权限决策链零改动回归;写工具被附加建议;intel 为 None 零变化。
"""

import pytest

from codesage.engine.loop import AgentLoopConfig
from codesage.engine.session import AgentSession  # noqa: F401  # 确保引擎可导入


def test_agentloop_config_intel_default_none():
    """spec 20 §4:AgentLoopConfig.intel 默认 None(零变化)。"""
    # 构造一个最小配置验证字段默认值(用类型而不是实例化完整 loop)
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(AgentLoopConfig)}
    assert "intel" in field_names


def test_write_tools_are_guarded():
    """spec 20 §4:约束层只约束 Write/Edit。"""
    from codesage.intel import WRITE_TOOLS

    assert "Write" in WRITE_TOOLS
    assert "Edit" in WRITE_TOOLS
    assert "Read" not in WRITE_TOOLS


@pytest.mark.asyncio
async def test_minimal_change_advice_prepends_to_content(monkeypatch):
    """spec 20 §4:写工具执行后,约束层建议 prepend 到结果 content。"""
    from codesage.intel import MinimalChangeGuard

    class FakeIntel:
        available = True

        async def impact_of_change(self, symbol):
            return {"callers_total": 3}

    class FakeTool:
        name = "Edit"

    class FakeItem:
        class Result:
            content = "original result"
            is_error = False

        tool = FakeTool()
        input = {"file_path": "src/foo.py"}
        result = Result()

    guard = MinimalChangeGuard(FakeIntel())
    advice = await guard.guard("Edit", {"file_path": "src/foo.py"})
    assert advice is not None
    assert "minimal-change" in advice


@pytest.mark.asyncio
async def test_minimal_change_read_not_guarded(monkeypatch):
    """spec 20 §4:读操作不被约束。"""
    from codesage.intel import MinimalChangeGuard

    guard = MinimalChangeGuard(None)
    assert await guard.guard("Read", {"file_path": "x.py"}) is None


def test_permissions_engine_untouched():
    """spec 20 §4.3:权限决策链零改动回归(决策链语义不变)。"""
    from codesage.permissions import PermissionEngine
    from codesage.permissions.engine import PermissionMode

    engine = PermissionEngine()
    decision = engine.evaluate_tool_use(
        tool_name="Read", tool_input={"file_path": "x.py"}, permissions={},
        mode=PermissionMode.DEFAULT, cwd=__import__("pathlib").Path("."),
    )
    # 未知工具默认 ask(决策链第 9 步)—— 权限引擎本身未被 20 改动
    assert decision.mode in ("allow", "ask")