"""权限/审批层(06-P6): 独立 services/permission/, 引擎权限语义收口于此。

布局:
- runtime.py    RuntimePermissionRuntime(运行时工具权限裁决: 系统工具豁免/
                permission_rules 显式规则/plan-mode 只读门/skill allowed_tools 收敛)
- guardrails.py 写盘/Shell 审批簿(register/has/consume + guardrails 总开关),
                纯运行时状态操作, 不触发审批 UI(approval 闭环属特性工作项, 06 不接线)
"""
from app.services.permission.runtime import (
    READ_ONLY_RUNTIME_TOOL_NAMES,
    SYSTEM_RUNTIME_TOOL_NAMES,
    RuntimePermissionRuntime,
    ToolPermissionDecision,
    resolve_permission_rule_decision,
)
from app.services.permission.guardrails import (
    GUARDRAILS_METADATA_KEY,
    SHELL_APPROVALS_METADATA_KEY,
    WRITE_APPROVALS_METADATA_KEY,
    consume_shell_approval,
    consume_write_approval,
    has_shell_approval,
    has_write_approval,
    is_guardrails_enabled,
    register_shell_approval,
    register_write_approval,
    set_guardrails_enabled,
)

__all__ = [
    "GUARDRAILS_METADATA_KEY",
    "READ_ONLY_RUNTIME_TOOL_NAMES",
    "RuntimePermissionRuntime",
    "SHELL_APPROVALS_METADATA_KEY",
    "SYSTEM_RUNTIME_TOOL_NAMES",
    "ToolPermissionDecision",
    "WRITE_APPROVALS_METADATA_KEY",
    "consume_shell_approval",
    "consume_write_approval",
    "has_shell_approval",
    "has_write_approval",
    "is_guardrails_enabled",
    "register_shell_approval",
    "register_write_approval",
    "resolve_permission_rule_decision",
    "set_guardrails_enabled",
]
