"""Permission engine (phase 05): decision chain, rules, modes, audit, persistence."""

from .audit import JsonlAuditSink, NullAuditSink, ToolAuditEvent
from .engine import PermissionDecision, PermissionEngine
from .modes import (
    READ_ONLY_TOOLS,
    REQUIRES_EXPLICIT_APPROVAL,
    SYSTEM_TOOLS,
    PermissionMode,
    normalize_mode,
)
from .store import load_permission_rules, save_approval

__all__ = [
    "JsonlAuditSink",
    "NullAuditSink",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionMode",
    "READ_ONLY_TOOLS",
    "REQUIRES_EXPLICIT_APPROVAL",
    "SYSTEM_TOOLS",
    "ToolAuditEvent",
    "load_permission_rules",
    "normalize_mode",
    "save_approval",
]
