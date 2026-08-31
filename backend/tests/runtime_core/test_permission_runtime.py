from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.runtime_core.permission_runtime import RuntimePermissionRuntime
from app.services.runtime_core.tool_runtime import ToolExecutionContext


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def build_context(store: AuditSessionStore) -> tuple[str, str, ToolExecutionContext]:
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    context = ToolExecutionContext(
        session_id=session_id,
        turn_id=turn_id,
        tool_use_id="tool-use-1",
        tool_call_id="tool-call-1",
        agent_type="finding",
    )
    return session_id, turn_id, context


def test_permission_runtime_allows_system_tools_during_plan_mode():
    store = build_store()
    session_id, _, context = build_context(store)
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.permission_mode = "plan"
    store.replace_runtime_state(session_id, runtime_state)

    runtime = RuntimePermissionRuntime(session_store=store, agent_type="finding")
    decision = runtime.evaluate_tool_use(tool_name="AskUser", context=context)

    assert decision.allowed is True
    assert decision.mode == "allow"
    assert decision.source == "runtime"


def test_permission_runtime_blocks_non_readonly_tools_in_plan_mode():
    store = build_store()
    session_id, _, context = build_context(store)
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.permission_mode = "plan"
    store.replace_runtime_state(session_id, runtime_state)

    runtime = RuntimePermissionRuntime(session_store=store, agent_type="finding")
    decision = runtime.evaluate_tool_use(tool_name="Write", context=context)

    assert decision.allowed is False
    assert decision.mode == "deny"
    assert decision.source == "permission_mode"
    assert "plan mode" in (decision.reason or "").lower()


def test_permission_runtime_supports_explicit_ask_rules():
    store = build_store()
    session_id, _, context = build_context(store)
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["permission_rules"] = {
        "finding": {
            "Write": {"mode": "ask", "reason": "Need approval before mutating state."}
        }
    }
    store.replace_runtime_state(session_id, runtime_state)

    runtime = RuntimePermissionRuntime(session_store=store, agent_type="finding")
    decision = runtime.evaluate_tool_use(tool_name="Write", context=context)

    assert decision.allowed is False
    assert decision.mode == "ask"
    assert decision.source == "permission_rule"
    assert "approval" in (decision.reason or "").lower()


def test_permission_runtime_respects_skill_allowed_tools_after_explicit_rules():
    store = build_store()
    session_id, _, context = build_context(store)
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.record_skill_contract(
        agent_type="finding",
        skill_ref="code-audit-finding",
        contract={"allowed_tools": ["Read"]},
    )
    store.replace_runtime_state(session_id, runtime_state)

    runtime = RuntimePermissionRuntime(session_store=store, agent_type="finding")
    denied = runtime.evaluate_tool_use(tool_name="Write", context=context)
    allowed = runtime.evaluate_tool_use(tool_name="Read", context=context)

    assert denied.allowed is False
    assert denied.source == "skill_allowed_tools"
    assert allowed.allowed is True
    assert allowed.source == "runtime"
