"""MCP 工具权限联动测试(spec 12.1:test_mcp_permissions.py)。

覆盖 MCP 工具在权限引擎决策链中的行为(spec §7.3,引擎零改动):
needs_permissions 恒 True 不走 self-declared / unknown 默认 ask / 精确与通配规则 /
deny 恒胜 / plan 模式拒 / 审计恰一条。
"""

import pytest

from codesage.permissions import PermissionEngine, PermissionMode
from codesage.tools import Tool

#: MCP 工具名样例(echo 服务器)
MCP_TOOL = "mcp__echo__echo"


class FakeMcpTool(Tool):
    """模拟 McpTool:needs_permissions 恒 True(spec 裁决 3)。"""

    def __init__(self, name=MCP_TOOL):
        self.name = name
        self.description = ""

    def needs_permissions(self, input):
        return True  # MCP 工具恒需权限,决策权在引擎


def _engine(**rules) -> PermissionEngine:
    return PermissionEngine()


def _decide(tool, *, mode="default", permissions=None, **kw):
    eng = PermissionEngine()
    return eng.evaluate_tool_use(
        tool_name=tool.name,
        tool_input={},
        tool=tool,
        permissions=permissions or {},
        mode=mode,
        cwd=__import__("pathlib").Path("."),
        **kw,
    )


def test_mcp_tool_not_in_system_whitelist():
    """spec §7.3:MCP 工具不进 SYSTEM_TOOLS(白名单是 harness 内部工具)。"""
    from codesage.permissions import SYSTEM_TOOLS

    assert MCP_TOOL not in SYSTEM_TOOLS


def test_mcp_tool_needs_permissions_true_so_not_self_declared():
    """spec 裁决 3:needs_permissions 恒 True → 不走 self-declared 自动放行。"""
    tool = FakeMcpTool()
    decision = _decide(tool)
    assert decision.mode == "ask"  # 未放行,走默认 ask


def test_mcp_tool_exact_allow_rule():
    """spec §7.3:精确规则 allow。"""
    tool = FakeMcpTool()
    decision = _decide(tool, permissions={"allow": [MCP_TOOL]})
    assert decision.mode == "allow"


def test_mcp_tool_wildcard_allow_rule():
    """spec §7.3:通配规则 allow(mcp__echo__*)。"""
    tool = FakeMcpTool()
    decision = _decide(tool, permissions={"allow": ["mcp__echo__*"]})
    assert decision.mode == "allow"


def test_mcp_tool_deny_always_wins():
    """spec §7.3:deny 恒胜(即使同时 allow)。"""
    tool = FakeMcpTool()
    decision = _decide(tool, permissions={"allow": [MCP_TOOL], "deny": [MCP_TOOL]})
    assert decision.mode == "deny"


def test_mcp_tool_plan_mode_blocked():
    """spec §7.3:MCP 工具非只读,plan 模式下被拒。"""
    tool = FakeMcpTool()
    decision = _decide(tool, mode="plan")
    assert decision.mode == "deny"
    assert decision.source == "plan-mode"


def test_mcp_tool_audit_single_event():
    """spec §7.3:每次决策恰一条审计事件。"""
    events: list = []

    class MemSink:
        def emit(self, event):
            events.append(event)

    eng = PermissionEngine(audit_sink=MemSink())
    tool = FakeMcpTool()
    eng.evaluate_tool_use(tool_name=tool.name, tool_input={}, tool=tool, permissions={})
    assert len(events) == 1


def test_mcp_tool_yolo_auto_allows():
    """spec §7.3:yolo 自动放行(deny 除外)。"""
    tool = FakeMcpTool()
    decision = _decide(tool, mode="yolo")
    assert decision.mode == "allow"