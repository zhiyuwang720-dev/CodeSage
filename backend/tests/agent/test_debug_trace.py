from datetime import datetime, timezone

from app.api.v1.endpoints import agent_tasks


def _event(
    *,
    event_id: str,
    event_type: str,
    sequence: int,
    agent_name: str,
    agent_type: str,
    message: str = "",
    phase: str | None = None,
    metadata: dict | None = None,
    tool_name: str | None = None,
):
    return {
        "id": event_id,
        "event_type": event_type,
        "sequence": sequence,
        "phase": phase,
        "message": message,
        "tool_name": tool_name,
        "tool_input": None,
        "tool_output": None,
        "tool_duration_ms": None,
        "created_at": datetime(2026, 3, 18, 12, sequence, tzinfo=timezone.utc),
        "event_metadata": {
            "agent_name": agent_name,
            "agent_type": agent_type,
            **(metadata or {}),
        },
    }


def test_build_debug_trace_payload_groups_handoffs_and_agents():
    events = [
        _event(
            event_id="1",
            event_type="prompt_system",
            sequence=1,
            agent_name="Recon",
            agent_type="recon",
            message="system",
            metadata={"payload": {"content": "system prompt"}},
        ),
        _event(
            event_id="2",
            event_type="handoff_out",
            sequence=2,
            agent_name="Recon",
            agent_type="recon",
            message="handoff",
            metadata={
                "payload": {
                    "from_agent": "recon",
                    "to_agent": "scan",
                    "summary": "focus scan on auth",
                }
            },
        ),
        _event(
            event_id="3",
            event_type="tool_call",
            sequence=3,
            agent_name="Scan",
            agent_type="scan",
            message="calling tool",
            tool_name="semgrep_scan",
            metadata={"payload": {"tool_name": "semgrep_scan"}},
        ),
    ]

    payload = agent_tasks.build_debug_trace_payload(
        task_id="task-1",
        task_name="Task",
        task_status="running",
        events=events,
    )

    assert payload["task"]["id"] == "task-1"
    assert payload["summary"]["event_count"] == 3
    assert payload["summary"]["agents"] == ["recon", "scan"]
    assert payload["summary"]["tool_calls"] == 1
    assert payload["summary"]["handoff_count"] == 1
    assert payload["handoffs"][0]["from_agent"] == "recon"
    assert payload["handoffs"][0]["to_agent"] == "scan"
    assert payload["timeline"][0]["payload"]["content"] == "system prompt"
    assert payload["timeline"][2]["tool_name"] == "semgrep_scan"


def test_build_debug_task_item_uses_latest_event_timestamp():
    item = agent_tasks.build_debug_task_item(
        task_id="task-2",
        task_name="Flow",
        project_id="project-1",
        status="completed",
        created_at=datetime(2026, 3, 18, 1, 0, tzinfo=timezone.utc),
        events=[
            _event(
                event_id="a",
                event_type="agent_start",
                sequence=1,
                agent_name="Recon",
                agent_type="recon",
            ),
            _event(
                event_id="b",
                event_type="tool_call",
                sequence=2,
                agent_name="Finding",
                agent_type="finding",
                tool_name="read_file",
            ),
        ],
    )

    assert item["id"] == "task-2"
    assert item["event_count"] == 2
    assert item["agent_count"] == 2
    assert item["tool_call_count"] == 1
    assert item["latest_event_at"].endswith("+00:00")
