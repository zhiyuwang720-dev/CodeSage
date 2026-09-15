from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GUARDRAILS_METADATA_KEY = "guardrails"
WRITE_APPROVALS_METADATA_KEY = "write_approvals"
SHELL_APPROVALS_METADATA_KEY = "shell_approvals"
APPROVAL_SCOPE_SINGLE_USE = "single_use"
APPROVAL_SCOPE_SESSION = "session"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_relative_path(path: str) -> str:
    return Path(str(path or "").strip()).as_posix().lstrip("./")


def normalize_command(command: str) -> str:
    return " ".join(str(command or "").strip().split())


def normalize_approval_scope(scope: str | None) -> str:
    normalized = str(scope or "").strip().lower()
    if normalized == APPROVAL_SCOPE_SINGLE_USE:
        return APPROVAL_SCOPE_SINGLE_USE
    return APPROVAL_SCOPE_SESSION


def _approval_is_available(item: dict[str, Any]) -> bool:
    scope = normalize_approval_scope(str(item.get("scope") or ""))
    if scope == APPROVAL_SCOPE_SINGLE_USE and item.get("consumed_at"):
        return False
    return True


def is_guardrails_enabled(runtime_state: Any) -> bool:
    metadata = dict(getattr(runtime_state, "metadata", {}) or {})
    config = dict(metadata.get(GUARDRAILS_METADATA_KEY) or {})
    return bool(config.get("enabled") is True)


def set_guardrails_enabled(runtime_state: Any, enabled: bool) -> bool:
    metadata = runtime_state.metadata.setdefault(GUARDRAILS_METADATA_KEY, {})
    metadata["enabled"] = bool(enabled)
    metadata["updated_at"] = _utc_now()
    return bool(metadata["enabled"])


def register_write_approval(
    runtime_state: Any,
    *,
    path: str,
    guardrail_code: str,
    tool_call_id: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    normalized_path = normalize_relative_path(path)
    normalized_scope = normalize_approval_scope(scope)
    approvals = runtime_state.metadata.setdefault(WRITE_APPROVALS_METADATA_KEY, [])
    for item in approvals:
        if (
            normalize_relative_path(str(item.get("path") or "")) == normalized_path
            and str(item.get("guardrail_code") or "") == str(guardrail_code or "")
            and normalize_approval_scope(str(item.get("scope") or "")) == normalized_scope
        ):
            item["approved_at"] = _utc_now()
            item["scope"] = normalized_scope
            item.pop("consumed_at", None)
            if tool_call_id:
                item["tool_call_id"] = tool_call_id
            return dict(item)
    record = {
        "path": normalized_path,
        "guardrail_code": str(guardrail_code or "").strip(),
        "scope": normalized_scope,
        "tool_call_id": str(tool_call_id or "").strip() or None,
        "approved_at": _utc_now(),
    }
    approvals.append(record)
    return dict(record)


def has_write_approval(
    runtime_state: Any,
    *,
    project_root: Path,
    resolved_path: Path,
    guardrail_code: str,
) -> bool:
    approvals = runtime_state.metadata.get(WRITE_APPROVALS_METADATA_KEY) or []
    try:
        relative_path = resolved_path.relative_to(project_root).as_posix()
    except ValueError:
        relative_path = str(resolved_path)
    normalized_relative_path = normalize_relative_path(relative_path)
    for item in approvals:
        if normalize_relative_path(str(item.get("path") or "")) != normalized_relative_path:
            continue
        if str(item.get("guardrail_code") or "") != str(guardrail_code or ""):
            continue
        if not _approval_is_available(item):
            continue
        return True
    return False


def consume_write_approval(
    runtime_state: Any,
    *,
    project_root: Path,
    resolved_path: Path,
    guardrail_code: str,
) -> dict[str, Any] | None:
    approvals = runtime_state.metadata.get(WRITE_APPROVALS_METADATA_KEY) or []
    try:
        relative_path = resolved_path.relative_to(project_root).as_posix()
    except ValueError:
        relative_path = str(resolved_path)
    normalized_relative_path = normalize_relative_path(relative_path)
    for item in approvals:
        if normalize_relative_path(str(item.get("path") or "")) != normalized_relative_path:
            continue
        if str(item.get("guardrail_code") or "") != str(guardrail_code or ""):
            continue
        if not _approval_is_available(item):
            continue
        if normalize_approval_scope(str(item.get("scope") or "")) == APPROVAL_SCOPE_SINGLE_USE:
            item["consumed_at"] = _utc_now()
        return dict(item)
    return None


def register_shell_approval(
    runtime_state: Any,
    *,
    tool_name: str,
    command: str,
    guardrail_code: str,
    tool_call_id: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    normalized_tool_name = str(tool_name or "").strip()
    normalized_command = normalize_command(command)
    normalized_scope = normalize_approval_scope(scope)
    approvals = runtime_state.metadata.setdefault(SHELL_APPROVALS_METADATA_KEY, [])
    for item in approvals:
        if str(item.get("tool_name") or "") != normalized_tool_name:
            continue
        if normalize_command(str(item.get("command") or "")) != normalized_command:
            continue
        if str(item.get("guardrail_code") or "") != str(guardrail_code or ""):
            continue
        if normalize_approval_scope(str(item.get("scope") or "")) != normalized_scope:
            continue
        item["approved_at"] = _utc_now()
        item["scope"] = normalized_scope
        item.pop("consumed_at", None)
        if tool_call_id:
            item["tool_call_id"] = tool_call_id
        return dict(item)
    record = {
        "tool_name": normalized_tool_name,
        "command": normalized_command,
        "guardrail_code": str(guardrail_code or "").strip(),
        "scope": normalized_scope,
        "tool_call_id": str(tool_call_id or "").strip() or None,
        "approved_at": _utc_now(),
    }
    approvals.append(record)
    return dict(record)


def has_shell_approval(
    runtime_state: Any,
    *,
    tool_name: str,
    command: str,
    guardrail_code: str,
) -> bool:
    normalized_tool_name = str(tool_name or "").strip()
    normalized_command = normalize_command(command)
    approvals = runtime_state.metadata.get(SHELL_APPROVALS_METADATA_KEY) or []
    for item in approvals:
        if str(item.get("tool_name") or "") != normalized_tool_name:
            continue
        if normalize_command(str(item.get("command") or "")) != normalized_command:
            continue
        if str(item.get("guardrail_code") or "") != str(guardrail_code or ""):
            continue
        if not _approval_is_available(item):
            continue
        return True
    return False


def consume_shell_approval(
    runtime_state: Any,
    *,
    tool_name: str,
    command: str,
    guardrail_code: str,
) -> dict[str, Any] | None:
    normalized_tool_name = str(tool_name or "").strip()
    normalized_command = normalize_command(command)
    approvals = runtime_state.metadata.get(SHELL_APPROVALS_METADATA_KEY) or []
    for item in approvals:
        if str(item.get("tool_name") or "") != normalized_tool_name:
            continue
        if normalize_command(str(item.get("command") or "")) != normalized_command:
            continue
        if str(item.get("guardrail_code") or "") != str(guardrail_code or ""):
            continue
        if not _approval_is_available(item):
            continue
        if normalize_approval_scope(str(item.get("scope") or "")) == APPROVAL_SCOPE_SINGLE_USE:
            item["consumed_at"] = _utc_now()
        return dict(item)
    return None
