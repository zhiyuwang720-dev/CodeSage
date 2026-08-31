from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.runtime_core.session_state import (
    AgentRuntimeState,
    InvokedSkillState,
    SessionRuntimeState,
    build_legacy_agent_runtime_state,
    sync_legacy_agent_metadata_from_runtime_state,
)


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_session_runtime_state_tracks_progressive_skill_loading_per_agent():
    state = SessionRuntimeState(session_id="session-1")

    finding_state = state.ensure_agent_state("finding")
    assert finding_state.agent_type == "finding"
    assert finding_state.invoked_skills == {}

    first = state.mark_skill_invoked(
        agent_type="finding",
        skill_ref="code-audit-finding",
        skill_stage="body",
        invocation_id="inv-1",
        turn_id="turn-1",
    )
    second = state.mark_skill_invoked(
        agent_type="finding",
        skill_ref="code-audit-finding",
        skill_stage="references",
        invocation_id="inv-2",
        turn_id="turn-2",
    )
    third = state.mark_skill_invoked(
        agent_type="verification",
        skill_ref="verification-skill",
        skill_stage="body",
        invocation_id="inv-3",
        turn_id="turn-2",
    )

    assert first.skill_stage == "body"
    assert second.skill_stage == "references"
    assert second.invocation_count == 2
    assert second.last_invocation_id == "inv-2"
    assert second.last_turn_id == "turn-2"
    assert state.agent_states["finding"].invoked_skills["code-audit-finding"].skill_stage == "references"
    assert state.agent_states["verification"].invoked_skills["verification-skill"].skill_stage == "body"
    assert state.list_invoked_skills("finding") == ["code-audit-finding"]


def test_session_store_persists_and_reloads_runtime_state_round_trip():
    store = build_store()
    session_id = store.create_session(project_id="project-1", runtime_stack="runtime-core")

    runtime_state = SessionRuntimeState(
        session_id=session_id,
        permission_mode="plan",
        touched_paths=["backend/app/auth/service.py"],
        pending_questions=[{"id": "ask-1", "question": "Need approval?"}],
        agent_states={
            "finding": AgentRuntimeState(
                agent_type="finding",
                invoked_skills={
                    "code-audit-finding": InvokedSkillState(
                        skill_ref="code-audit-finding",
                        skill_stage="references",
                        invocation_count=2,
                        first_invoked_at="2026-04-08T09:00:00+00:00",
                        last_invoked_at="2026-04-08T09:05:00+00:00",
                        last_invocation_id="inv-2",
                        last_turn_id="turn-2",
                        loaded_resources=["SKILL.md", "references/checklists/python.md"],
                    )
                },
            )
        },
    )

    store.replace_runtime_state(session_id, runtime_state)
    loaded = store.load_runtime_state(session_id)

    assert loaded is not None
    assert loaded.session_id == session_id
    assert loaded.permission_mode == "plan"
    assert loaded.touched_paths == ["backend/app/auth/service.py"]
    assert loaded.pending_questions[0]["id"] == "ask-1"
    assert loaded.agent_states["finding"].invoked_skills["code-audit-finding"].skill_stage == "references"
    assert loaded.agent_states["finding"].invoked_skills["code-audit-finding"].invocation_count == 2


def test_legacy_session_runtime_adapter_round_trips_permissions_and_hooks():
    interaction_state = {
        "permission_mode": "plan",
        "pending_todos": [{"id": "todo-1", "title": "Review auth flow"}],
        "pending_questions": [{"id": "question-1", "question": "Need approval?"}],
        "plan_mode": {"active": True, "reason": "Need review before mutation"},
        "todos": {"todo-1": {"id": "todo-1", "title": "Review auth flow"}},
        "questions": {"question-1": {"id": "question-1", "question": "Need approval?"}},
        "permission_rules": {
            "mutating_probe": {"mode": "ask", "reason": "Need human approval before mutation."}
        },
    }
    tool_runtime = {
        "records": [{"tool_name": "TodoWrite", "status": "completed"}],
        "events": [{"event": "PostToolUse", "tool_name": "TodoWrite"}],
        "hook_records": [{"event": "PostToolUse", "tool_name": "TodoWrite", "skill_ref": "code-audit-finding"}],
        "checkpoints": [{"checkpoint_type": "auto", "state_payload": {"event": "PostToolUse", "tool_name": "TodoWrite"}}],
        "session_hooks": {
            "code-audit-finding": {
                "PostToolUse": [{"matcher": "*", "hooks": ["log-post"]}]
            }
        },
    }

    runtime_state = build_legacy_agent_runtime_state(
        session_id="agent-1",
        agent_type="recon",
        interaction_state=interaction_state,
        tool_runtime=tool_runtime,
    )

    assert runtime_state.permission_mode == "plan"
    assert runtime_state.metadata["permission_rules"]["mutating_probe"]["mode"] == "ask"
    assert runtime_state.metadata["session_hooks"]["code-audit-finding"]["PostToolUse"][0]["hooks"] == ["log-post"]
    assert runtime_state.agent_states["recon"].pending_todos[0]["title"] == "Review auth flow"

    interaction_store = {}
    tool_store = {}
    sync_legacy_agent_metadata_from_runtime_state(
        runtime_state,
        agent_type="recon",
        interaction_state=interaction_store,
        tool_runtime=tool_store,
    )

    assert interaction_store["permission_mode"] == "plan"
    assert interaction_store["permission_rules"]["mutating_probe"]["mode"] == "ask"
    assert interaction_store["plan_mode"]["active"] is True
    assert tool_store["session_hooks"]["code-audit-finding"]["PostToolUse"][0]["hooks"] == ["log-post"]
    assert tool_store["records"][-1]["tool_name"] == "TodoWrite"


def test_legacy_session_runtime_adapter_preserves_memory_runtime_payload():
    interaction_state = {
        "permission_mode": "default",
    }
    tool_runtime = {"records": []}
    memory_runtime = {
        "base_system_prompt": "Base prompt",
        "instructions": [
            {
                "memory_kind": "instruction",
                "title": "Project rule",
                "source_type": "project_memory",
                "source_ref": "CLAUDE.md",
                "content": "Focus auth flows.",
                "relevance_score": None,
                "metadata": {"scope": "project"},
            }
        ],
        "recalls": [],
        "source": "task-bootstrap",
    }

    runtime_state = build_legacy_agent_runtime_state(
        session_id="agent-1",
        agent_type="analysis",
        interaction_state=interaction_state,
        tool_runtime=tool_runtime,
        memory_runtime=memory_runtime,
    )

    assert runtime_state.metadata["memory_runtime"]["instructions"][0]["source_ref"] == "CLAUDE.md"

    interaction_store = {}
    tool_store = {}
    memory_store = {}
    sync_legacy_agent_metadata_from_runtime_state(
        runtime_state,
        agent_type="analysis",
        interaction_state=interaction_store,
        tool_runtime=tool_store,
        memory_runtime=memory_store,
    )

    assert memory_store["base_system_prompt"] == "Base prompt"
    assert memory_store["instructions"][0]["title"] == "Project rule"
    assert memory_store["source"] == "task-bootstrap"
