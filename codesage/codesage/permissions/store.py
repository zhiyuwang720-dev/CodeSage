"""Rule persistence: read settings.permissions, persist approvals.

Approvals land in settings.local.json (project-local, never committed) —
matching Kode's savePermission flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_permission_rules(settings: Any) -> dict[str, Any]:
    """Pull the permissions dict from a Settings object (phase 01)."""
    return getattr(settings, "permissions", {}) or {}


def save_approval(local_settings_path: Path, tool_name: str, rule_value: str) -> None:
    """Append a tool rule to settings.local.json's permissions.allow."""
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
    local_settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = local_settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(local_settings_path)
