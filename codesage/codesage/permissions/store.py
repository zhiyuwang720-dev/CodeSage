"""Rule persistence: read settings.permissions, persist approvals.

Approvals land in settings.local.json (project-local, never committed) —
matching Kode's savePermission flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import atomic_write


def load_permission_rules(settings: Any) -> dict[str, Any]:
    """Pull the permissions dict from a Settings object (phase 01)."""
    return getattr(settings, "permissions", {}) or {}


def build_rule_string(tool_name: str, tool_input: dict[str, Any] | None) -> str:
    """The allow rule to persist for an approved tool use (granular remember):

    - Bash → "Bash(<command, first 80 chars, whitespace-normalized>)"
    - Edit/Write → "Edit(<parent dir>/**)" (parent directory, recursive)
    - every other tool → bare tool name
    """
    tool_input = tool_input or {}
    if tool_name == "Bash":
        command = " ".join(str(tool_input.get("command") or "").split())
        return f"Bash({command[:80]})"
    if tool_name in ("Edit", "Write"):
        raw = tool_input.get("file_path") or tool_input.get("path")
        if raw:
            parent = str(Path(str(raw)).parent).replace("\\", "/")
            if parent not in ("", "."):
                return f"{tool_name}({parent}/**)"
    return tool_name


def build_session_rule(tool_name: str, tool_input: dict[str, Any] | None) -> str:
    """Same rule string as build_rule_string, but marked session-only: the
    caller puts it in a SessionRuleStore, it is never persisted, and it dies
    with the session."""
    return build_rule_string(tool_name, tool_input)


class SessionRuleStore:
    """In-memory session grants (never persisted; dies with the session).

    rules() output is directly usable as session_permissions — the engine
    merges it after the settings rules, and `!rule` negations revoke them.
    """

    def __init__(self) -> None:
        self._rules: dict[str, list[str]] = {"allow": [], "deny": [], "ask": []}

    def allow(self, rule: str) -> None:
        """Add an allow rule (idempotent)."""
        if rule not in self._rules["allow"]:
            self._rules["allow"].append(rule)

    def rules(self) -> dict[str, list[str]]:
        """{allow: [...], deny: [...], ask: [...]} — session_permissions shape."""
        return {key: list(values) for key, values in self._rules.items()}


def save_approval(local_settings_path: Path, tool_name: str, rule: str | None = None) -> None:
    """Append an allow rule to settings.local.json's permissions.allow.

    rule defaults to the bare tool name (backwards compatible); callers
    persisting a granular grant pass a build_rule_string(...) result.
    """
    rule_value = rule if rule is not None else tool_name
    existing: dict[str, Any] = {}
    if local_settings_path.exists():
        try:
            existing = json.loads(local_settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    permissions = existing.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    if rule_value not in allow:
        allow.append(rule_value)
    atomic_write(local_settings_path, json.dumps(existing, ensure_ascii=False, indent=2))
