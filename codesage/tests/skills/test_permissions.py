"""技能权限联动测试(阶段 14 S4):引擎第 8.5 步授权 + 无参数回归。

决策链零改动红线(§7.2/§12.2):``skill_allowed_tools`` 是**可选参数**,默认
None 时逐位行为与 13 交付一致(回归断言);授权只豁免「无规则无地板时的
默认 ask」—— deny/ask 规则、写保护、工作目录、敏感路径、显式批准(Bash)、
plan 模式全部在前(§7.1 前置约束全胜);每次决策恰一条审计事件。
"""

from pathlib import Path

from codesage.permissions import PermissionEngine, PermissionMode
from codesage.permissions.audit import ToolAuditEvent
from codesage.permissions.modes import READ_ONLY_TOOLS
from codesage.tools import Tool


class _CollectSink:
    def __init__(self):
        self.events: list[ToolAuditEvent] = []

    def emit(self, event: ToolAuditEvent) -> None:
        self.events.append(event)


class FakeNormalTool(Tool):
    name = "NormalTool"


#: 技能授权集(§7.1):把「默认 ask」升级为 allow
_GRANT = frozenset({"NormalTool"})


def _eval(tool_name="NormalTool", *, grant=None, **kw):
    return PermissionEngine().evaluate_tool_use(
        tool_name=tool_name,
        tool_input={"command": "x"},
        tool=FakeNormalTool(),
        skill_allowed_tools=frozenset(grant) if grant is not None else None,
        **kw,
    )


# ---- 无参数回归:默认 None 与 13 逐位一致 ----

def test_no_grant_behavior_identical_to_default():
    """不带 skill_allowed_tools 参数 = 显式 None = 13 决策链逐位一致。"""
    plain = PermissionEngine().evaluate_tool_use(
        tool_name="NormalTool", tool_input={"command": "x"}, tool=FakeNormalTool()
    )
    with_grant_none = _eval(grant=None)
    assert (plain.allowed, plain.mode, plain.reason, plain.source) == (
        with_grant_none.allowed, with_grant_none.mode, with_grant_none.reason, with_grant_none.source,
    )
    assert plain.mode == "ask"  # 未知工具默认 ask(不变量)


def test_grant_empty_set_is_noop():
    assert _eval(grant=()).mode == "ask"  # 空授权集 = 无授权


# ---- 授权豁免默认 ask ----

def test_grant_exempts_default_ask():
    d = _eval(grant=_GRANT)
    assert d.allowed and d.mode == "allow"
    assert d.source == "skill-allowed-tools"


def test_grant_does_not_cover_other_tools():
    d = _eval(tool_name="OtherTool", grant=_GRANT)
    assert d.mode == "ask"  # 授权只覆盖列出的工具名


# ---- 前置约束全胜(§7.1)----

def test_deny_rule_wins_over_grant():
    d = _eval(permissions={"deny": ["NormalTool"]}, grant=_GRANT)
    assert d.mode == "deny" and not d.allowed


def test_ask_rule_wins_over_grant():
    d = _eval(permissions={"ask": ["NormalTool"]}, grant=_GRANT)
    assert d.mode == "ask" and not d.allowed


def test_write_protection_wins_over_grant(tmp_path):
    # 写保护路径(.codesage 组件,静态地板):即使授权也不豁免
    target = tmp_path / ".codesage" / "settings.json"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write",
        tool_input={"file_path": str(target), "content": "x"},
        cwd=tmp_path,
        skill_allowed_tools=frozenset({"Write"}),
    )
    assert d.mode == "ask" and d.requires_explicit_approval
    assert d.source == "write-protection"


def test_working_dir_wins_over_grant(tmp_path):
    target = tmp_path.parent / "outside.txt"
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Write",
        tool_input={"file_path": str(target), "content": "x"},
        cwd=tmp_path,
        skill_allowed_tools=frozenset({"Write"}),
    )
    assert d.mode == "ask" and d.requires_explicit_approval
    assert d.source == "working-dir"


def test_sensitive_path_wins_over_grant(tmp_path):
    """敏感/写保护地板在前:Read .env 需显式批准,授权不豁免。

    注:is_sensitive ⊆ is_write_protected(静态集),敏感路径在决策链第 4 步
    写保护地板即被拦(source=write-protection),第 6 步敏感分支被掩盖 ——
    断言的是「保护地板胜」这一语义(非授权放行)。
    """
    target = tmp_path / ".env"
    target.write_text("KEY=value\n")
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Read",
        tool_input={"file_path": str(target)},
        cwd=tmp_path,
        skill_allowed_tools=frozenset({"Read"}),
    )
    assert d.mode == "ask" and d.requires_explicit_approval
    assert d.source != "skill-allowed-tools"


def test_requires_explicit_approval_bash_wins_over_grant():
    """REQUIRES_EXPLICIT_APPROVAL(Bash):技能 allowed-tools: [Bash] 不豁免显式批准。"""
    d = PermissionEngine().evaluate_tool_use(
        tool_name="Bash",
        tool_input={"command": "ls"},
        skill_allowed_tools=frozenset({"Bash"}),
    )
    assert d.mode == "ask" and d.requires_explicit_approval
    assert d.source == "explicit-approval"


def test_plan_mode_wins_over_grant():
    """plan 模式:技能授权不绕过(§7.3 成文)。"""
    d = PermissionEngine().evaluate_tool_use(
        tool_name="NormalTool",
        tool_input={"command": "x"},
        tool=FakeNormalTool(),
        mode=PermissionMode.PLAN,
        skill_allowed_tools=_GRANT,
    )
    assert d.mode == "deny" and not d.allowed
    assert d.source == "plan-mode"


def test_yolo_grant_redundant_but_allows():
    """yolo 天然全放行(第 8 步先返回),授权冗余无害。"""
    d = _eval(mode=PermissionMode.YOLO, grant=_GRANT)
    assert d.allowed and d.source == "yolo"


# ---- 审计:恰一条事件 ----

def test_grant_audits_single_event():
    sink = _CollectSink()
    engine = PermissionEngine(audit_sink=sink)
    engine.evaluate_tool_use(
        tool_name="NormalTool", tool_input={"command": "x"}, tool=FakeNormalTool(),
        skill_allowed_tools=_GRANT,
    )
    assert len(sink.events) == 1  # 每决策恰一条(05 不变量)
    ev = sink.events[0]
    assert ev.source == "skill-allowed-tools"
    assert ev.decision == "allow"
    assert ev.tool_name == "NormalTool"


def test_deny_rule_audits_deny_even_with_grant():
    """deny 规则在前:即使授权也存在,审计照常记 deny(恰一条)。"""
    sink = _CollectSink()
    engine = PermissionEngine(audit_sink=sink)
    engine.evaluate_tool_use(
        tool_name="NormalTool", tool_input={"command": "x"}, tool=FakeNormalTool(),
        permissions={"deny": ["NormalTool"]},
        skill_allowed_tools=_GRANT,
    )
    assert len(sink.events) == 1
    assert sink.events[0].decision == "deny"
