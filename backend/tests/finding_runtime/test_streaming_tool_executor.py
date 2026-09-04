from __future__ import annotations

import asyncio

from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.audit_session import AuditToolCallStatus
from app.services.contracts.models import ToolCallRequest, ToolExecutionPayload
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.tooling.runtime import RuntimeTool, ToolExecutionContext, ToolOrchestrator, ToolRegistry


class EchoInput(BaseModel):
    text: str


class StreamingConcurrentTool(RuntimeTool):
    description = "Concurrent streaming tool"
    input_model = EchoInput

    def __init__(self, *, name: str, delay: float = 0.01, fail: bool = False):
        self.name = name
        self._delay = delay
        self._fail = fail

    def is_concurrency_safe(self, parsed_input: EchoInput) -> bool:
        return True

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        context.report_progress(event="progress", message=f"start:{parsed_input.text}")
        await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError(f"{parsed_input.text}-boom")
        context.report_progress(event="progress", message=f"end:{parsed_input.text}")
        return ToolExecutionPayload(
            content=f"echo:{parsed_input.text}",
            output_payload={"echo": parsed_input.text},
            context_modifier={"seen": {parsed_input.text: True}},
        )


class SerialModifierTool(RuntimeTool):
    name = "SerialTool"
    description = "Serial modifier tool"
    input_model = EchoInput

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        return ToolExecutionPayload(
            content=f"serial:{parsed_input.text}",
            output_payload={"echo": parsed_input.text},
            context_modifier={parsed_input.text: True},
        )


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_streaming_tool_executor_yields_progress_before_records_and_applies_concurrent_context_after_batch():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry([
        StreamingConcurrentTool(name="ReadA", delay=0.03),
        StreamingConcurrentTool(name="ReadB", delay=0.01),
    ])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry)

    async def collect_updates():
        executor = orchestrator.build_streaming_executor(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[
                ToolCallRequest(id="tool-1", name="ReadA", input={"text": "alpha"}),
                ToolCallRequest(id="tool-2", name="ReadB", input={"text": "beta"}),
            ],
            initial_context={"seed": True},
        )
        updates = []
        async for update in executor.get_remaining_updates():
            updates.append(update)
        return updates, executor.get_updated_context()

    updates, updated_context = asyncio.run(collect_updates())

    progress_indexes = [index for index, update in enumerate(updates) if update.kind == "progress"]
    record_indexes = [index for index, update in enumerate(updates) if update.kind == "record"]
    assert progress_indexes
    assert record_indexes
    assert min(progress_indexes) < min(record_indexes)
    assert [update.record.request.id for update in updates if update.kind == "record"] == ["tool-1", "tool-2"]
    context_updates = [update for update in updates if update.kind == "context"]
    assert len(context_updates) == 1
    assert context_updates[0].new_context == {"seed": True, "seen": {"alpha": True, "beta": True}}
    assert updated_context == {"seed": True, "seen": {"alpha": True, "beta": True}}


def test_streaming_tool_executor_applies_serial_context_modifier_after_each_tool():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry([SerialModifierTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry)

    async def collect_updates():
        executor = orchestrator.build_streaming_executor(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[
                ToolCallRequest(id="tool-1", name="SerialTool", input={"text": "first"}),
                ToolCallRequest(id="tool-2", name="SerialTool", input={"text": "second"}),
            ],
        )
        updates = []
        async for update in executor.get_remaining_updates():
            updates.append(update)
        return updates

    updates = asyncio.run(collect_updates())

    context_updates = [update.new_context for update in updates if update.kind == "context"]
    assert context_updates == [{"first": True}, {"first": True, "second": True}]


def test_streaming_tool_executor_cancels_sibling_tools_after_shell_error():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry([
        StreamingConcurrentTool(name="Bash", delay=0.01, fail=True),
        StreamingConcurrentTool(name="ReadA", delay=0.2),
    ])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry)

    async def collect_updates():
        executor = orchestrator.build_streaming_executor(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[
                ToolCallRequest(id="tool-1", name="Bash", input={"text": "boom"}),
                ToolCallRequest(id="tool-2", name="ReadA", input={"text": "alpha"}),
            ],
        )
        updates = []
        async for update in executor.get_remaining_updates():
            updates.append(update)
        return updates

    updates = asyncio.run(collect_updates())

    records = [update.record for update in updates if update.kind == "record"]
    assert [record.status for record in records] == [AuditToolCallStatus.FAILED.value, AuditToolCallStatus.FAILED.value]
    assert records[0].result.metadata["error_kind"] == "execution_error"
    assert records[1].result.metadata["error_kind"] == "interrupted"
