from __future__ import annotations

from types import SimpleNamespace

from app.services.review_runtime.models import RuntimeMessageRole, ToolCallRequest, ToolExecutionPayload, ToolExecutionRecord, TranscriptItem
from app.services.runtime_core.hook_policy import evaluate_post_tool_hook_policy, evaluate_stop_hook_policy


def test_evaluate_stop_hook_policy_merges_runtime_hook_blocking_errors():
    runtime_state = SimpleNamespace(metadata={"stop_hooks": {}})
    messages = [
        TranscriptItem(role=RuntimeMessageRole.USER, content="inspect code"),
        TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="done"),
    ]

    result = evaluate_stop_hook_policy(
        runtime_state=runtime_state,
        messages=messages,
        model_response=SimpleNamespace(stop_reason="completed", content="done"),
        hook_events=[
            {
                "event": "Stop",
                "matched_hooks": [
                    {"blocking_error": "Need concrete sink evidence before stopping."}
                ],
            }
        ],
    )

    assert result["blocking_errors"] == ["Need concrete sink evidence before stopping."]
    assert result["prevent_continuation"] is False


def test_evaluate_post_tool_hook_policy_stops_when_runtime_hook_prevents_continuation():
    runtime_state = SimpleNamespace(metadata={"stop_hooks": {}})
    records = [
        ToolExecutionRecord(
            tool_call_id="call-1",
            request=ToolCallRequest(id="tool-1", name="Read", input={}),
            status="completed",
            is_concurrency_safe=True,
            result=ToolExecutionPayload(content="ok", is_error=False),
            error_message=None,
            duration_ms=5,
        )
    ]

    result = evaluate_post_tool_hook_policy(
        runtime_state=runtime_state,
        records=records,
        hook_events=[
            {
                "event": "PostToolUse",
                "matched_hooks": [
                    {
                        "prevent_continuation": True,
                        "stop_reason": "Skill requested stop after successful tool execution.",
                    }
                ],
            }
        ],
    )

    assert result["hook_stopped"] is True
    assert result["stop_reason"] == "Skill requested stop after successful tool execution."


def test_evaluate_stop_hook_policy_emits_task_completed_blocking_for_teammate_context():
    runtime_state = SimpleNamespace(
        metadata={
            "stop_hooks": {},
            "teammate": {
                "enabled": True,
                "agent_name": "alice",
                "team_name": "red",
                "tasks": [
                    {
                        "id": "task-1",
                        "subject": "Trace auth sink",
                        "description": "Need proof for privilege escalation sink",
                        "owner": "alice",
                        "status": "in_progress",
                    }
                ],
            },
            "session_hooks": {
                "code-audit-finding": {
                    "TaskCompleted": [
                        {
                            "matcher": "*",
                            "blocking_error": "Confirm whether the auth sink is actually reachable before concluding the task is done.",
                        }
                    ]
                }
            },
        }
    )

    result = evaluate_stop_hook_policy(
        runtime_state=runtime_state,
        messages=[TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="done")],
        model_response=SimpleNamespace(stop_reason="completed", content="done"),
        hook_events=[],
    )

    assert result["blocking_errors"] == [
        "Confirm whether the auth sink is actually reachable before concluding the task is done."
    ]
    assert result["prevent_continuation"] is False
    assert result["emitted_hook_events"][0]["event"] == "TaskCompleted"
    assert result["emitted_hook_events"][0]["task_id"] == "task-1"


def test_evaluate_stop_hook_policy_emits_teammate_idle_prevent_continuation():
    runtime_state = SimpleNamespace(
        metadata={
            "stop_hooks": {},
            "teammate": {
                "enabled": True,
                "agent_name": "alice",
                "team_name": "red",
            },
            "session_hooks": {
                "code-audit-finding": {
                    "TeammateIdle": [
                        {
                            "matcher": "*",
                            "prevent_continuation": True,
                            "stop_reason": "Teammate idle hook requested a handoff before continuing.",
                        }
                    ]
                }
            },
        }
    )

    result = evaluate_stop_hook_policy(
        runtime_state=runtime_state,
        messages=[TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="done")],
        model_response=SimpleNamespace(stop_reason="completed", content="done"),
        hook_events=[],
    )

    assert result["blocking_errors"] == []
    assert result["prevent_continuation"] is True
    assert result["stop_reason"] == "Teammate idle hook requested a handoff before continuing."
    assert result["emitted_hook_events"][0]["event"] == "TeammateIdle"
