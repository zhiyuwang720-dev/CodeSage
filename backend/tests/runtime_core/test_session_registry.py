from app.services.runtime_core.session_registry import runtime_session_registry
from app.services.runtime_core.session_state import SessionRuntimeState


def setup_function():
    runtime_session_registry.clear()


def test_runtime_session_registry_tracks_legacy_snapshots_by_agent_and_task():
    state = SessionRuntimeState(session_id="session-1", permission_mode="plan")
    runtime_session_registry.upsert(
        session_key="legacy:task-1:agent-1",
        runtime_state=state,
        agent_id="agent-1",
        agent_type="recon",
        task_id="task-1",
        source="legacy",
    )

    by_key = runtime_session_registry.get("legacy:task-1:agent-1")
    by_agent = runtime_session_registry.get_by_agent("agent-1")
    task_items = runtime_session_registry.list_by_task("task-1")

    assert by_key is not None
    assert by_key["source"] == "legacy"
    assert by_key["task_id"] == "task-1"
    assert by_key["runtime_state"]["permission_mode"] == "plan"
    assert by_agent is not None
    assert by_agent["session_key"] == "legacy:task-1:agent-1"
    assert task_items[0]["session_key"] == "legacy:task-1:agent-1"
