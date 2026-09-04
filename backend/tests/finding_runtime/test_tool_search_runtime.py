from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.contracts.models import ToolExecutionPayload
from app.services.session.store import AuditSessionStore
from app.services.tooling.runtime import ToolExecutionContext, ToolRegistry, build_runtime_tool
from app.services.tooling.search import ToolSearchInput, ToolSearchRuntimeTool


async def _noop_execute(parsed_input, context):
    del parsed_input, context
    return ToolExecutionPayload(content="ok", output_payload={})


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return AuditSessionStore(session_factory=sessionmaker(bind=engine))


def test_tool_search_runtime_selects_and_ranks_deferred_tools():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    registry = ToolRegistry()
    registry.register(
        build_runtime_tool(
            name="Read",
            description="Read files",
            execute=_noop_execute,
        )
    )
    registry.register(
        build_runtime_tool(
            name="AskUser",
            description="Ask a human for clarification",
            execute=_noop_execute,
            should_defer=True,
            search_hint="ask the user a question",
        )
    )
    registry.register(
        build_runtime_tool(
            name="TodoWrite",
            description="Record a todo",
            execute=_noop_execute,
            should_defer=True,
            search_hint="record a todo or plan step",
        )
    )
    tool = ToolSearchRuntimeTool(session_store=store, registry_getter=lambda: registry)
    registry.register(tool)
    context = ToolExecutionContext(session_id=session_id, turn_id="turn-1", tool_use_id="tool-1", tool_call_id="call-1")

    selected = asyncio.run(tool.execute(ToolSearchInput(query="select:AskUser"), context))
    searched = asyncio.run(tool.execute(ToolSearchInput(query="todo plan"), context))

    assert selected.output_payload["matches"] == ["AskUser"]
    assert selected.output_payload["total_deferred_tools"] == 2
    assert searched.output_payload["matches"] == ["TodoWrite"]
