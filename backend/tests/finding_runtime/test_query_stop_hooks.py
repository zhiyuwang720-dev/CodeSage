from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.contracts.models import RuntimeMessageRole, ToolCallRequest, ToolExecutionPayload, ToolExecutionRecord, TranscriptItem
from app.services.runtime.query_stop_hooks import build_stop_hook_artifact_messages, evaluate_post_tool_hooks, evaluate_stop_hooks


class FakeExecutorRuntime:
    def __init__(self, *, event_result: dict | None = None):
        self._event_result = dict(event_result or {})

    async def execute_event_hooks(self, event: dict):
        executed = dict(event)
        executed.update(self._event_result)
        return executed


def test_evaluate_stop_hooks_blocks_when_claim_lacks_tool_evidence():
    runtime_state = SimpleNamespace(
        metadata={
            "stop_hooks": {
                "require_tool_result_evidence": True,
                "claim_phrases": ["reportable finding", "definitely exploitable"],
                "missing_evidence_message": "Need concrete tool-backed evidence before claiming a reportable finding.",
            }
        }
    )
    messages = [
        TranscriptItem(role=RuntimeMessageRole.USER, content="inspect code"),
        TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="This is definitely exploitable and looks like a reportable finding."),
    ]

    result = asyncio.run(
        evaluate_stop_hooks(
            runtime_state=runtime_state,
            messages=messages,
            model_response=SimpleNamespace(stop_reason="completed", content=messages[-1].content),
        )
    )

    assert result["blocking_errors"] == ["Need concrete tool-backed evidence before claiming a reportable finding."]
    assert result["prevent_continuation"] is False


def test_evaluate_post_tool_hooks_can_stop_on_tool_error_when_configured():
    runtime_state = SimpleNamespace(metadata={"stop_hooks": {"stop_on_tool_error": True}})
    records = [
        ToolExecutionRecord(
            tool_call_id="call-1",
            request=ToolCallRequest(id="tool-1", name="echo", input={}),
            status="failed",
            is_concurrency_safe=True,
            result=ToolExecutionPayload(content="boom", is_error=True),
            error_message="boom",
            duration_ms=10,
        )
    ]

    result = asyncio.run(evaluate_post_tool_hooks(runtime_state=runtime_state, records=records))

    assert result["hook_stopped"] is True


def test_build_stop_hook_artifact_messages_emits_progress_attachment_and_summary():
    artifact_messages = build_stop_hook_artifact_messages(
        {
            "emitted_hook_events": [
                {
                    "event": "TaskCompleted",
                    "agent_name": "alice",
                    "team_name": "red",
                    "task_id": "task-1",
                    "task_subject": "Trace auth sink",
                }
            ],
            "stop_reason": "TaskCompleted hook prevented continuation",
            "prevent_continuation": True,
            "blocking_errors": [],
        }
    )

    assert [item.name for item in artifact_messages] == [
        "hook_progress",
        "hook_stopped_continuation",
        "stop_hook_summary",
    ]
    assert artifact_messages[0].payload["hook_event"] == "TaskCompleted"
    assert artifact_messages[1].metadata["hidden_from_model"] is True


def test_build_stop_hook_artifact_messages_includes_hook_telemetry_fields():
    artifact_messages = build_stop_hook_artifact_messages(
        {
            "emitted_hook_events": [
                {
                    "event": "TeammateIdle",
                    "agent_name": "alice",
                    "team_name": "red",
                    "hook_runs": [
                        {
                            "hookId": "idle-1",
                            "hookName": "TeammateIdle",
                            "command": "python hooks/idle.py",
                            "promptText": "Decide whether alice should hand off.",
                            "stdout": "handoff requested",
                            "stderr": "",
                            "exitCode": 0,
                            "durationMs": 42,
                            "content": "handoff requested",
                            "outcome": "success",
                            "response": None,
                        }
                    ],
                    "hook_execution_events": [
                        {
                            "type": "started",
                            "hookId": "idle-1",
                            "hookName": "TeammateIdle",
                            "hookEvent": "TeammateIdle",
                            "command": "python hooks/idle.py",
                            "promptText": "Decide whether alice should hand off.",
                        },
                        {
                            "type": "progress",
                            "hookId": "idle-1",
                            "hookName": "TeammateIdle",
                            "hookEvent": "TeammateIdle",
                            "stdout": "handoff requested",
                            "stderr": "",
                            "output": "handoff requested",
                            "command": "python hooks/idle.py",
                            "promptText": "Decide whether alice should hand off.",
                        },
                    ],
                }
            ],
            "stop_reason": "",
            "prevent_continuation": False,
            "blocking_errors": [],
        }
    )

    assert [item.name for item in artifact_messages] == [
        "hook_progress",
        "hook_progress",
        "hook_attachment",
        "stop_hook_summary",
    ]
    assert artifact_messages[0].payload["data"]["command"] == "python hooks/idle.py"
    assert artifact_messages[1].payload["data"]["output"] == "handoff requested"
    assert artifact_messages[2].payload["attachment_type"] == "hook_success"
    assert artifact_messages[3].payload["hook_infos"][0]["durationMs"] == 42
    assert artifact_messages[3].payload["has_output"] is True
