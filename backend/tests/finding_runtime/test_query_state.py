from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.review_runtime.models import RuntimeContinueReason, RuntimeMessageRole, TranscriptItem
from app.services.review_runtime.query_state import QueryLoopState
from app.services.review_runtime.session_store import AuditSessionStore


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_query_loop_state_round_trips_through_payload():
    state = QueryLoopState(
        messages=[TranscriptItem(role=RuntimeMessageRole.USER, content="inspect repo")],
        tool_use_context={"permission_mode": "default"},
        auto_compact_tracking={"attempted": True},
        context_collapse_state={
            "commits": [
                {
                    "collapse_id": "0000000000000001",
                    "summary_uuid": "summary-1",
                    "summary_content": "<collapsed id=\"1\">Earlier context</collapsed>",
                    "summary": "Earlier context",
                    "first_archived_uuid": "u1",
                    "last_archived_uuid": "u2",
                }
            ],
            "snapshot": {"staged": [], "armed": False, "last_spawn_tokens": 0},
        },
        max_output_tokens_recovery_count=2,
        has_attempted_reactive_compact=True,
        max_output_tokens_override=64000,
        pending_tool_use_summary={"status": "pending"},
        stop_hook_active=True,
        turn_count=3,
        transition=RuntimeContinueReason.NEXT_TURN,
    )

    restored = QueryLoopState.from_payload(state.to_payload())

    assert restored.messages[0].role is RuntimeMessageRole.USER
    assert restored.messages[0].content == "inspect repo"
    assert restored.tool_use_context == {"permission_mode": "default"}
    assert restored.auto_compact_tracking == {"attempted": True}
    assert restored.context_collapse_state["commits"][0]["summary_uuid"] == "summary-1"
    assert restored.max_output_tokens_recovery_count == 2
    assert restored.has_attempted_reactive_compact is True
    assert restored.max_output_tokens_override == 64000
    assert restored.pending_tool_use_summary == {"status": "pending"}
    assert restored.stop_hook_active is True
    assert restored.turn_count == 3
    assert restored.transition is RuntimeContinueReason.NEXT_TURN


def test_session_store_persists_query_loop_state_in_runtime_state_json():
    store = build_store()
    session_id = store.create_session(project_id="project-1", runtime_stack="runtime")
    state = QueryLoopState(
        messages=[TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="Need a tool")],
        tool_use_context={"agent": "finding"},
        auto_compact_tracking={"attempted": False},
        context_collapse_state={
            "commits": [],
            "snapshot": {
                "staged": [{"start_uuid": "u1", "end_uuid": "u2", "summary": "Earlier context", "risk": 5, "staged_at": 1}],
                "armed": True,
                "last_spawn_tokens": 42,
            },
        },
        turn_count=2,
        transition=RuntimeContinueReason.NEXT_TURN,
    )

    store.save_query_loop_state(session_id, state)

    loaded = store.load_query_loop_state(session_id)
    shared_runtime_state = store.load_runtime_state(session_id)

    assert loaded.turn_count == 2
    assert loaded.transition is RuntimeContinueReason.NEXT_TURN
    assert loaded.messages[0].role is RuntimeMessageRole.ASSISTANT
    assert loaded.messages[0].content == "Need a tool"
    assert loaded.context_collapse_state["snapshot"]["staged"][0]["summary"] == "Earlier context"
    assert shared_runtime_state.metadata["query_loop"]["turn_count"] == 2
    assert shared_runtime_state.metadata["query_loop"]["transition"] == RuntimeContinueReason.NEXT_TURN.value
