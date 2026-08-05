"""Permission modes and the read-only tool set (design note #5/#6)."""

from __future__ import annotations

from enum import Enum


class PermissionMode(str, Enum):
    PLAN = "plan"  # read-only until user approves execution
    DEFAULT = "default"  # ask for unknown/side-effecting tools
    YOLO = "yolo"  # auto-allow what would ask, never bypassing explicit approval


def normalize_mode(mode: str | PermissionMode | None) -> PermissionMode:
    try:
        return PermissionMode((mode or "default").strip().lower())
    except ValueError:
        return PermissionMode.DEFAULT


#: Tools that never touch state; allowed in plan mode and exempt from yolo's ask.
READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS"})

#: Internal harness tools (not model-visible) — always allowed.
# Skill is NOT whitelisted: model-invoked skills run through the normal chain.
SYSTEM_TOOLS = frozenset({"TodoWrite", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode"})

#: Tools that are never auto-allowed even in yolo mode (require explicit approval).
REQUIRES_EXPLICIT_APPROVAL = frozenset({"Bash"})
