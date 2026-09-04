from __future__ import annotations

from typing import Any

from app.services.contracts.models import RuntimeMessageRole, TranscriptItem
from app.services.runtime_core.hook_policy import evaluate_post_tool_hook_policy, evaluate_stop_hook_policy
from app.services.runtime_core.hook_runtime import HookExecutorRuntime


async def evaluate_stop_hooks(
    *,
    runtime_state: Any,
    messages: list[TranscriptItem],
    model_response: Any,
    hook_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = evaluate_stop_hook_policy(
        runtime_state=runtime_state,
        messages=messages,
        model_response=model_response,
        hook_events=hook_events,
    )
    executed_events = await _execute_hook_events(result.get("emitted_hook_events") or [])
    return _merge_executed_hook_result(result, executed_events)


def build_stop_hook_messages(blocking_errors: list[str]) -> list[TranscriptItem]:
    return [
        TranscriptItem(
            role=RuntimeMessageRole.USER,
            content=error,
            name="stop_hook_blocking",
            metadata={"synthetic": True, "kind": "stop_hook_blocking"},
        )
        for error in blocking_errors
        if str(error or "").strip()
    ]


def _hook_runs(event: dict[str, Any]) -> list[dict[str, Any]]:
    explicit_runs = [dict(item) for item in (event.get("hook_runs") or []) if isinstance(item, dict)]
    if explicit_runs:
        return explicit_runs

    runs = []
    matched_hooks = event.get("matched_hooks") or []
    for index, hook in enumerate(matched_hooks, start=1):
        if not isinstance(hook, dict):
            continue
        command = str(hook.get("command") or hook.get("cmd") or "").strip() or None
        prompt_text = str(hook.get("prompt_text") or hook.get("promptText") or "").strip() or None
        stdout = str(hook.get("stdout") or "")
        stderr = str(hook.get("stderr") or "")
        exit_code = hook.get("exit_code")
        duration_ms = hook.get("duration_ms")
        content = str(hook.get("content") or "").strip() or None
        if content is None and stdout.strip():
            content = stdout.strip()
        runs.append(
            {
                "index": index,
                "command": command,
                "promptText": prompt_text,
                "stdout": stdout,
                "stderr": stderr,
                "exitCode": int(exit_code) if exit_code is not None else None,
                "durationMs": int(duration_ms) if duration_ms is not None else None,
                "content": content,
                "outcome": "success" if exit_code in (None, 0) else "error",
                "response": None,
            }
        )
    if runs:
        return runs
    return [
        {
            "index": 1,
            "command": None,
            "promptText": None,
            "stdout": "",
            "stderr": "",
            "exitCode": None,
            "durationMs": None,
            "content": None,
            "outcome": "success",
            "response": None,
        }
    ]


def build_stop_hook_artifact_messages(result: dict[str, Any]) -> list[TranscriptItem]:
    artifacts: list[TranscriptItem] = []
    hook_events = [dict(item) for item in (result.get("emitted_hook_events") or []) if isinstance(item, dict)]
    hook_infos: list[dict[str, Any]] = []
    hook_errors: list[str] = []
    has_output = False
    last_tool_use_id: str | None = None
    last_hook_event: str | None = None
    hook_count = 0

    for event in hook_events:
        hook_event = str(event.get("event") or "").strip() or "Stop"
        last_hook_event = hook_event
        execution_events = [dict(item) for item in (event.get("hook_execution_events") or []) if isinstance(item, dict)]
        for index, run in enumerate(_hook_runs(event), start=1):
            tool_use_id = str(event.get("tool_use_id") or f"hook-{hook_event.lower()}-{hook_count + 1}").strip()
            event["tool_use_id"] = tool_use_id
            last_tool_use_id = tool_use_id
            hook_count += 1
            hook_name = str(run.get("hookName") or hook_event)
            if run.get("command"):
                hook_infos.append(
                    {
                        "command": run.get("command"),
                        "promptText": run.get("promptText"),
                        "durationMs": run.get("durationMs"),
                    }
                )
            relevant_events = [
                item
                for item in execution_events
                if str(item.get("hookId") or "") == str(run.get("hookId") or "") or not run.get("hookId")
            ]
            if not relevant_events:
                progress_bits = [f"Running {hook_event} hook"]
                if event.get("agent_name"):
                    progress_bits.append(f"for {event['agent_name']}")
                if event.get("task_subject"):
                    progress_bits.append(f"on {event['task_subject']}")
                artifacts.append(
                    TranscriptItem(
                        role=RuntimeMessageRole.SYSTEM,
                        content=" ".join(progress_bits),
                        name="hook_progress",
                        metadata={"synthetic": True, "kind": "hook_progress", "hidden_from_model": True},
                        payload={
                            "hook_event": hook_event,
                            "tool_use_id": tool_use_id,
                            "hook_name": hook_name,
                            "data": {
                                "command": run.get("command"),
                                "promptText": run.get("promptText"),
                                "stdout": run.get("stdout"),
                                "stderr": run.get("stderr"),
                                "output": str(run.get("stdout") or "") + str(run.get("stderr") or ""),
                            },
                        },
                    )
                )
            for execution_event in relevant_events:
                if execution_event.get("type") in {"started", "progress"}:
                    progress_payload = {
                        "command": execution_event.get("command") or run.get("command"),
                        "promptText": execution_event.get("promptText") or run.get("promptText"),
                        "stdout": execution_event.get("stdout"),
                        "stderr": execution_event.get("stderr"),
                        "output": execution_event.get("output"),
                    }
                    artifacts.append(
                        TranscriptItem(
                            role=RuntimeMessageRole.SYSTEM,
                            content=str(execution_event.get("output") or execution_event.get("stdout") or execution_event.get("stderr") or f"Running {hook_event} hook"),
                            name="hook_progress",
                            metadata={"synthetic": True, "kind": "hook_progress", "hidden_from_model": True},
                            payload={
                                "hook_event": hook_event,
                                "tool_use_id": tool_use_id,
                                "hook_name": hook_name,
                                "data": progress_payload,
                            },
                        )
                    )
            stdout = str(run.get("stdout") or "")
            stderr = str(run.get("stderr") or "")
            exit_code = run.get("exitCode")
            duration_ms = run.get("durationMs")
            content = str(run.get("content") or "")
            attachment_type = "hook_success"
            if str(run.get("outcome") or "").strip().lower() == "cancelled":
                attachment_type = "hook_error_during_execution"
            elif stderr.strip() and exit_code not in (None, 0):
                attachment_type = "hook_non_blocking_error"
            elif str(content).strip() and not run.get("command") and not stdout and not stderr and exit_code is None:
                attachment_type = "hook_error_during_execution"
            if stdout.strip() or stderr.strip() or content.strip() or exit_code is not None:
                has_output = True
            if attachment_type == "hook_non_blocking_error":
                hook_errors.append(stderr or f"Exit code {exit_code}")
            elif attachment_type == "hook_error_during_execution" and content.strip():
                hook_errors.append(content)
            if stdout.strip() or stderr.strip() or content.strip() or exit_code is not None:
                artifacts.append(
                    TranscriptItem(
                        role=RuntimeMessageRole.SYSTEM,
                        content=content or stdout or stderr or f"{hook_event} hook completed",
                        name="hook_attachment",
                        metadata={"synthetic": True, "kind": "hook_attachment", "hidden_from_model": True},
                        payload={
                            "attachment_type": attachment_type,
                            "hook_event": hook_event,
                            "hook_name": hook_name,
                            "tool_use_id": tool_use_id,
                            "command": run.get("command"),
                            "durationMs": duration_ms,
                            "stdout": stdout,
                            "stderr": stderr,
                            "exitCode": exit_code,
                            "content": content,
                        },
                    )
                )

    stop_reason = str(result.get("stop_reason") or "").strip()
    if result.get("prevent_continuation") and stop_reason:
        artifacts.append(
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content=stop_reason,
                name="hook_stopped_continuation",
                metadata={"synthetic": True, "kind": "hook_stopped_continuation", "hidden_from_model": True},
                payload={
                    "hook_event": last_hook_event,
                    "tool_use_id": last_tool_use_id,
                    "message": stop_reason,
                },
            )
        )

    if hook_count > 0:
        summary = f"Stop hooks ran: {hook_count}"
        if stop_reason:
            summary = f"{summary}. stop_reason={stop_reason}"
        total_duration_ms = sum(int(item.get("durationMs") or 0) for item in hook_infos)
        artifacts.append(
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content=summary,
                name="stop_hook_summary",
                metadata={"synthetic": True, "kind": "stop_hook_summary", "hidden_from_model": True},
                payload={
                    "hook_count": hook_count,
                    "hook_infos": hook_infos,
                    "hook_errors": hook_errors,
                    "prevented_continuation": bool(result.get("prevent_continuation")),
                    "stop_reason": stop_reason or None,
                    "has_output": has_output,
                    "hook_event": last_hook_event,
                    "tool_use_id": last_tool_use_id,
                    "total_duration_ms": total_duration_ms or None,
                },
            )
        )
    return artifacts


async def evaluate_post_tool_hooks(
    *,
    runtime_state: Any,
    records: list[Any],
    hook_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    executed_events = await _execute_hook_events(hook_events or [])
    result = evaluate_post_tool_hook_policy(
        runtime_state=runtime_state,
        records=records,
        hook_events=executed_events,
    )
    result["emitted_hook_events"] = executed_events
    return result


async def _execute_hook_events(hook_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runtime = HookExecutorRuntime()
    executed_events: list[dict[str, Any]] = []
    for event in hook_events:
        executed_events.append(await runtime.execute_event_hooks(dict(event)))
    return executed_events


def _merge_executed_hook_result(base_result: dict[str, Any], executed_events: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(base_result)
    blocking_errors = list(result.get("blocking_errors") or [])
    prevent_continuation = bool(result.get("prevent_continuation"))
    stop_reason = str(result.get("stop_reason") or "").strip() or None
    additional_contexts: list[str] = []
    for event in executed_events:
        for error in event.get("blocking_errors") or []:
            if error not in blocking_errors:
                blocking_errors.append(error)
        for context in event.get("additional_contexts") or []:
            if context not in additional_contexts:
                additional_contexts.append(context)
        prevent_continuation = prevent_continuation or bool(event.get("prevent_continuation"))
        stop_reason = stop_reason or (str(event.get("stop_reason") or "").strip() or None)
    result["blocking_errors"] = blocking_errors if not prevent_continuation else []
    result["prevent_continuation"] = prevent_continuation
    result["stop_reason"] = stop_reason
    result["additional_contexts"] = additional_contexts
    result["emitted_hook_events"] = executed_events
    return result
