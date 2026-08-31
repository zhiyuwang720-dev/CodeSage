from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SYSTEM_RUNTIME_TOOL_NAMES = {"Skill", "TodoWrite", "AskUser", "EnterPlanMode", "ExitPlanMode"}
READ_ONLY_RUNTIME_TOOL_NAMES = {"Read", "Glob", "Grep", "Skill"}


@dataclass(slots=True)
class ToolPermissionDecision:
    allowed: bool
    reason: str | None = None
    source: str | None = None
    mode: str = "allow"
    guardrail_code: str | None = None


def resolve_permission_rule_decision(rules: Any, *, agent_type: str, tool_name: str) -> ToolPermissionDecision | None:
    if not isinstance(rules, dict):
        return None
    direct_rule = rules.get(tool_name)
    if isinstance(direct_rule, (str, dict)):
        scoped_rules = rules
    else:
        scoped_rules = rules.get(agent_type) or rules.get("session") or {}
    if not isinstance(scoped_rules, dict):
        return None
    raw_rule = scoped_rules.get(tool_name)
    if raw_rule is None:
        return None
    if isinstance(raw_rule, str):
        rule = {"mode": raw_rule}
    elif isinstance(raw_rule, dict):
        rule = raw_rule
    else:
        return None
    mode = str(rule.get("mode") or "allow").strip().lower()
    reason = str(rule.get("reason") or "").strip() or None
    if mode == "allow":
        return ToolPermissionDecision(allowed=True, source="permission_rule", mode="allow", reason=reason)
    if mode == "ask":
        return ToolPermissionDecision(
            allowed=False,
            source="permission_rule",
            mode="ask",
            reason=reason or f"Tool '{tool_name}' requires user approval before execution.",
        )
    return ToolPermissionDecision(
        allowed=False,
        source="permission_rule",
        mode="deny",
        reason=reason or f"Tool '{tool_name}' is denied by session permission rules.",
    )


class RuntimePermissionRuntime:
    def __init__(self, *, session_store, agent_type: str = "finding"):
        self._session_store = session_store
        self._agent_type = agent_type

    def evaluate_tool_use(self, *, tool_name: str, context: Any) -> ToolPermissionDecision:
        normalized_tool_name = str(tool_name or "").strip()
        if normalized_tool_name in SYSTEM_RUNTIME_TOOL_NAMES:
            return ToolPermissionDecision(allowed=True, source="runtime", mode="allow")

        runtime_state = self._session_store.load_runtime_state(context.session_id)
        explicit = resolve_permission_rule_decision(runtime_state.metadata.get("permission_rules"), agent_type=self._agent_type, tool_name=normalized_tool_name)
        if explicit is not None:
            return explicit

        permission_mode = str(runtime_state.permission_mode or "default").strip().lower()
        if permission_mode == "plan" and normalized_tool_name not in READ_ONLY_RUNTIME_TOOL_NAMES:
            return ToolPermissionDecision(
                allowed=False,
                reason=f"Tool '{normalized_tool_name}' is blocked in plan mode until the user approves execution.",
                source="permission_mode",
                mode="deny",
            )

        agent_state = runtime_state.agent_states.get(self._agent_type)
        if agent_state is None:
            return ToolPermissionDecision(allowed=True, source="runtime", mode="allow")

        runtime_metadata = agent_state.metadata.get("skill_runtime") or {}
        active_skills = runtime_metadata.get("active_skills") or {}
        allowed_tools: list[str] = []
        for contract in active_skills.values():
            for item in contract.get("allowed_tools") or []:
                normalized = str(item or "").strip()
                if normalized and normalized not in allowed_tools:
                    allowed_tools.append(normalized)
        if not allowed_tools or normalized_tool_name in allowed_tools:
            return ToolPermissionDecision(allowed=True, source="runtime", mode="allow")
        return ToolPermissionDecision(
            allowed=False,
            reason=f"Tool '{normalized_tool_name}' is not permitted by active skill allowed_tools: {', '.join(allowed_tools)}",
            source="skill_allowed_tools",
            mode="deny",
        )

