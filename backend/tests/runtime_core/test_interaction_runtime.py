from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.runtime_core.interaction_runtime import InteractionRuntime
from app.services.runtime_core.session_state import SessionRuntimeState


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_interaction_runtime_tracks_todos_questions_and_plan_mode_per_agent():
    state = SessionRuntimeState(session_id="session-1")
    runtime = InteractionRuntime()

    todo = runtime.create_todo(
        state,
        agent_type="finding",
        title="Review authz path",
        details="Trace owner check before approval sink",
    )
    question = runtime.ask_user(
        state,
        agent_type="finding",
        question="Need production config snapshot?",
        context={"candidate_id": "cand-1"},
    )
    runtime.enter_plan_mode(
        state,
        agent_type="orchestrator",
        reason="Need staged migration plan",
    )
    runtime.complete_todo(
        state,
        agent_type="finding",
        todo_id=todo["id"],
    )
    runtime.resolve_question(
        state,
        agent_type="finding",
        question_id=question["id"],
        answer="No",
    )
    runtime.exit_plan_mode(
        state,
        agent_type="orchestrator",
        reason="Plan approved",
    )

    finding_state = state.agent_states["finding"]

    assert finding_state.pending_todos[0]["status"] == "completed"
    assert finding_state.pending_todos[0]["title"] == "Review authz path"
    assert finding_state.pending_questions == []
    assert state.pending_questions == []
    assert state.permission_mode == "default"
    assert state.metadata["plan_mode"]["active"] is False
    assert state.metadata["plan_mode"]["owner_agent"] == "orchestrator"
    assert state.metadata["plan_mode"]["last_exit_reason"] == "Plan approved"
    assert state.metadata["questions"][question["id"]]["status"] == "answered"
    assert state.metadata["questions"][question["id"]]["answer"] == "No"


def test_interaction_runtime_round_trips_through_session_store():
    store = build_store()
    session_id = store.create_session(project_id="project-1", runtime_stack="runtime-core")
    state = store.load_runtime_state(session_id)
    runtime = InteractionRuntime()

    todo = runtime.create_todo(
        state,
        agent_type="verification",
        title="Confirm exploit preconditions",
    )
    question = runtime.ask_user(
        state,
        agent_type="verification",
        question="Can we use staging credentials?",
    )
    runtime.enter_plan_mode(
        state,
        agent_type="verification",
        reason="Need human decision before proceeding",
    )
    store.replace_runtime_state(session_id, state)

    loaded = store.load_runtime_state(session_id)

    assert loaded.permission_mode == "plan"
    assert loaded.agent_states["verification"].pending_todos[0]["id"] == todo["id"]
    assert loaded.pending_questions[0]["id"] == question["id"]
    assert loaded.metadata["plan_mode"]["active"] is True
    assert loaded.metadata["plan_mode"]["owner_agent"] == "verification"
