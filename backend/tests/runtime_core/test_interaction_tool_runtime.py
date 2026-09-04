from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.contracts.models import ToolCallRequest
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.runtime_core.tool_runtime import ToolOrchestrator, ToolRegistry
from app.services.agent.tools.ask_user_runtime_tool import AskUserRuntimeTool
from app.services.agent.tools.plan_mode_runtime_tool import EnterPlanModeRuntimeTool, ExitPlanModeRuntimeTool
from app.services.agent.tools.todo_runtime_tool import TodoWriteRuntimeTool


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_interaction_runtime_tools_update_session_runtime_state_via_orchestrator():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry(
        [
            TodoWriteRuntimeTool(store),
            AskUserRuntimeTool(store),
            EnterPlanModeRuntimeTool(store),
            ExitPlanModeRuntimeTool(store),
        ]
    )
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[
                ToolCallRequest(id="tool-1", name="TodoWrite", input={"title": "Inspect payment flow", "details": "trace refund approval"}),
                ToolCallRequest(id="tool-2", name="AskUser", input={"question": "Can we access staging secrets?"}),
                ToolCallRequest(id="tool-3", name="EnterPlanMode", input={"reason": "Need human decision before execution"}),
                ToolCallRequest(id="tool-4", name="ExitPlanMode", input={"reason": "Decision recorded"}),
            ],
        )
    )

    runtime_state = store.load_runtime_state(session_id)

    assert [record.status for record in records] == ["completed", "completed", "completed", "completed"]
    assert runtime_state.agent_states["finding"].pending_todos[0]["title"] == "Inspect payment flow"
    assert runtime_state.metadata["questions"]
    assert runtime_state.metadata["plan_mode"]["last_exit_reason"] == "Decision recorded"
    assert runtime_state.permission_mode == "default"
