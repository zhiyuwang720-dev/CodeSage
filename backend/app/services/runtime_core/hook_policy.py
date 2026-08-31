from __future__ import annotations

from typing import Any, Iterable

from app.services.review_runtime.models import RuntimeMessageRole, TranscriptItem
from app.services.runtime_core.tool_runtime import match_runtime_event_hooks


def collect_turn_hook_events(*, checkpoints: Iterable[Any], turn_id: str | None) -> list[dict[str, Any]]:
    if not turn_id:
        return []
    events: list[dict[str, Any]] = []
    for checkpoint in checkpoints or []:
        if str(getattr(checkpoint, "turn_id", "") or "") != str(turn_id):
            continue
        payload = dict(getattr(checkpoint, "state_payload", {}) or {})
        if payload.get("kind") != "runtime_hook":
            continue
        events.append(payload)
    return events


def _normalized_strings(values: list[Any] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in normalized:
            normalized.append(item)
    return normalized


def _iter_matched_hooks(hook_events: list[dict[str, Any]] | None):
    for event in hook_events or []:
        matched_hooks = event.get("matched_hooks") or []
        for hook in matched_hooks:
            if isinstance(hook, dict):
                yield event, hook


def _extract_blocking_errors(hook: dict[str, Any]) -> list[str]:
    errors = _normalized_strings(hook.get("blocking_errors"))
    single_error = str(hook.get("blocking_error") or hook.get("blockingError") or "").strip()
    if single_error and single_error not in errors:
        errors.append(single_error)
    if not errors and str(hook.get("action") or "").strip().lower() == "block":
        fallback = str(hook.get("message") or hook.get("stop_reason") or hook.get("stopReason") or "").strip()
        if fallback:
            errors.append(fallback)
    return errors


def _hook_prevents_continuation(hook: dict[str, Any]) -> bool:
    if bool(hook.get("prevent_continuation") or hook.get("preventContinuation")):
        return True
    action = str(hook.get("action") or "").strip().lower()
    return action in {"stop", "prevent_continuation", "prevent-continuation"}


def _hook_stop_reason(event: dict[str, Any], hook: dict[str, Any]) -> str | None:
    reason = str(hook.get("stop_reason") or hook.get("stopReason") or hook.get("message") or "").strip()
    if reason:
        return reason
    event_name = str(event.get("event") or "").strip()
    return f"{event_name or 'Runtime hook'} prevented continuation."


def _evaluate_hook_events(hook_events: list[dict[str, Any]] | None) -> tuple[list[str], bool, str | None]:
    blocking_errors: list[str] = []
    prevent_continuation = False
    stop_reason: str | None = None
    for event, hook in _iter_matched_hooks(hook_events):
        for error in _extract_blocking_errors(hook):
            if error not in blocking_errors:
                blocking_errors.append(error)
        if _hook_prevents_continuation(hook):
            prevent_continuation = True
            stop_reason = stop_reason or _hook_stop_reason(event, hook)
    return blocking_errors, prevent_continuation, stop_reason


def _normalize_teammate_tasks(runtime_state: Any, teammate_name: str) -> list[dict[str, Any]]:
    teammate = dict(getattr(runtime_state, "metadata", {}).get("teammate") or {})
    tasks = [dict(item) for item in teammate.get("tasks") or [] if isinstance(item, dict)]
    if tasks:
        return tasks
    fallback = []
    todos = dict(getattr(runtime_state, "metadata", {}).get("todos") or {})
    for item in todos.values():
        if not isinstance(item, dict):
            continue
        fallback.append(
            {
                "id": item.get("id"),
                "subject": item.get("title"),
                "description": item.get("details"),
                "owner": item.get("owner") or teammate_name,
                "status": item.get("status"),
            }
        )
    return fallback


def _build_teammate_hook_events(runtime_state: Any) -> list[dict[str, Any]]:
    metadata = getattr(runtime_state, "metadata", {}) or {}
    teammate = dict(metadata.get("teammate") or {})
    if not bool(teammate.get("enabled")):
        return []

    teammate_name = str(teammate.get("agent_name") or teammate.get("name") or "").strip()
    if not teammate_name:
        return []
    team_name = str(teammate.get("team_name") or teammate.get("team") or "").strip() or None
    session_hooks = dict(metadata.get("session_hooks") or {})
    emitted: list[dict[str, Any]] = []

    tasks = _normalize_teammate_tasks(runtime_state, teammate_name)
    for task in tasks:
        if str(task.get("status") or "").strip().lower() != "in_progress":
            continue
        if str(task.get("owner") or "").strip() != teammate_name:
            continue
        task_subject = str(task.get("subject") or task.get("title") or "").strip()
        for skill_ref, hook_config in session_hooks.items():
            matched_hooks = match_runtime_event_hooks(hook_config, event_name="TaskCompleted", tool_name=task_subject)
            if not matched_hooks:
                continue
            emitted.append(
                {
                    "kind": "runtime_hook",
                    "event": "TaskCompleted",
                    "skill_ref": skill_ref,
                    "matched_hooks": matched_hooks,
                    "agent_name": teammate_name,
                    "team_name": team_name,
                    "task_id": str(task.get("id") or "").strip() or None,
                    "task_subject": task_subject or None,
                    "task_description": str(task.get("description") or "").strip() or None,
                }
            )

    for skill_ref, hook_config in session_hooks.items():
        matched_hooks = match_runtime_event_hooks(hook_config, event_name="TeammateIdle", tool_name=teammate_name)
        if not matched_hooks:
            continue
        emitted.append(
            {
                "kind": "runtime_hook",
                "event": "TeammateIdle",
                "skill_ref": skill_ref,
                "matched_hooks": matched_hooks,
                "agent_name": teammate_name,
                "team_name": team_name,
            }
        )

    return emitted


def evaluate_stop_hook_policy(
    *,
    runtime_state: Any,
    messages: list[TranscriptItem],
    model_response: Any,
    hook_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stop_hooks = dict(getattr(runtime_state, "metadata", {}).get("stop_hooks") or {})
    blocking_errors = _normalized_strings(stop_hooks.get("blocking_errors") or [])

    if bool(stop_hooks.get("require_tool_result_evidence")):
        claim_phrases = [str(item).strip().lower() for item in stop_hooks.get("claim_phrases") or [] if str(item).strip()]
        response_content = str(getattr(model_response, "content", "") or "").strip()
        transcript_content = " ".join(str(getattr(message, "content", "") or "") for message in messages)
        combined_content = " ".join(part for part in [transcript_content, response_content] if part).lower()
        has_tool_result = any(getattr(message, "role", None) == RuntimeMessageRole.TOOL_RESULT for message in messages)
        has_claim = any(phrase and phrase in combined_content for phrase in claim_phrases)
        if has_claim and not has_tool_result:
            missing_evidence_message = str(
                stop_hooks.get("missing_evidence_message")
                or "Need concrete tool-backed evidence before claiming a reportable finding."
            ).strip()
            if missing_evidence_message and missing_evidence_message not in blocking_errors:
                blocking_errors.append(missing_evidence_message)

    prevent_continuation = bool(stop_hooks.get("prevent_continuation") or False)
    stop_reason = str(stop_hooks.get("stop_reason") or "").strip() or None
    hook_blocking, hook_prevent, hook_stop_reason = _evaluate_hook_events(hook_events)
    for error in hook_blocking:
        if error not in blocking_errors:
            blocking_errors.append(error)
    prevent_continuation = prevent_continuation or hook_prevent
    stop_reason = stop_reason or hook_stop_reason

    if prevent_continuation:
        return {
            "blocking_errors": [],
            "prevent_continuation": True,
            "stop_reason": stop_reason,
            "emitted_hook_events": [],
        }

    if blocking_errors:
        return {
            "blocking_errors": blocking_errors,
            "prevent_continuation": False,
            "stop_reason": stop_reason,
            "emitted_hook_events": [],
        }

    teammate_events = _build_teammate_hook_events(runtime_state)
    teammate_blocking, teammate_prevent, teammate_stop_reason = _evaluate_hook_events(teammate_events)
    if teammate_prevent:
        return {
            "blocking_errors": [],
            "prevent_continuation": True,
            "stop_reason": teammate_stop_reason,
            "emitted_hook_events": teammate_events,
        }
    if teammate_blocking:
        return {
            "blocking_errors": teammate_blocking,
            "prevent_continuation": False,
            "stop_reason": teammate_stop_reason,
            "emitted_hook_events": teammate_events,
        }

    return {
        "blocking_errors": [],
        "prevent_continuation": False,
        "stop_reason": stop_reason,
        "emitted_hook_events": teammate_events,
    }


def evaluate_post_tool_hook_policy(
    *,
    runtime_state: Any,
    records: list[Any],
    hook_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stop_hooks = dict(getattr(runtime_state, "metadata", {}).get("stop_hooks") or {})
    hook_stopped = bool(stop_hooks.get("hook_stopped") or False)
    stop_reason = str(stop_hooks.get("stop_reason") or "").strip() or None

    if not hook_stopped:
        for event, hook in _iter_matched_hooks(hook_events):
            if _hook_prevents_continuation(hook):
                hook_stopped = True
                stop_reason = stop_reason or _hook_stop_reason(event, hook)
                break

    if not hook_stopped and bool(stop_hooks.get("stop_on_tool_error")):
        hook_stopped = any(
            bool(getattr(getattr(record, "result", None), "is_error", False))
            or str(getattr(record, "status", "") or "").lower() != "completed"
            for record in records
        )
        if hook_stopped and not stop_reason:
            stop_reason = "Tool execution failed and stop_on_tool_error is enabled."

    return {
        "hook_stopped": hook_stopped,
        "stop_reason": stop_reason,
    }
