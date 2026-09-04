from __future__ import annotations

import asyncio

from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.audit_session import AuditToolCallStatus
from app.services.contracts.models import ToolCallRequest, ToolExecutionPayload
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.tooling.runtime import (
    RuntimeTool,
    ToolExecutionContext,
    ToolOrchestrator,
    ToolPermissionDecision,
    ToolRegistry,
)


class EchoInput(BaseModel):
    text: str


class ConcurrentEchoTool(RuntimeTool):
    name = "echo"
    description = "Echo text"
    input_model = EchoInput

    def __init__(self, events: list[tuple[str, str]]):
        self._events = events

    def is_concurrency_safe(self, parsed_input: EchoInput) -> bool:
        return True

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        self._events.append(("start", parsed_input.text))
        await asyncio.sleep(0.01)
        self._events.append(("end", parsed_input.text))
        return ToolExecutionPayload(
            content=f"echo:{parsed_input.text}",
            output_payload={"echo": parsed_input.text},
        )


class DeniedWriteTool(RuntimeTool):
    name = "write_file"
    description = "Denied write"
    input_model = EchoInput

    async def check_permission(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolPermissionDecision:
        return ToolPermissionDecision(allowed=False, reason="write access denied")

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        raise AssertionError("execute should not run when permission is denied")


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_tool_orchestrator_batches_concurrency_safe_tools_and_persists_results():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    events: list[tuple[str, str]] = []
    registry = ToolRegistry([ConcurrentEchoTool(events)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry)

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[
                ToolCallRequest(id="tool-1", name="echo", input={"text": "alpha"}),
                ToolCallRequest(id="tool-2", name="echo", input={"text": "beta"}),
            ],
        )
    )

    assert [record.status for record in records] == [AuditToolCallStatus.COMPLETED.value, AuditToolCallStatus.COMPLETED.value]
    assert events[:2] == [("start", "alpha"), ("start", "beta")]
    snapshot = store.load_session_snapshot(session_id)
    assert len(snapshot.tool_calls) == 2
    assert snapshot.tool_calls[0].is_concurrency_safe is True
    assert snapshot.tool_calls[1].output_payload == {"echo": "beta"}


def test_tool_orchestrator_records_permission_denials_without_running_tool():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry([DeniedWriteTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry)

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[ToolCallRequest(id="tool-1", name="write_file", input={"text": "secret"})],
        )
    )

    assert records[0].status == AuditToolCallStatus.DENIED.value
    assert records[0].error_message == "write access denied"
    snapshot = store.load_session_snapshot(session_id)
    assert snapshot.tool_calls[0].status == AuditToolCallStatus.DENIED.value
    assert snapshot.tool_calls[0].error_message == "write access denied"


class ProgressEchoTool(RuntimeTool):
    name = "progress_echo"
    description = "Echo with progress"
    input_model = EchoInput

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        context.report_progress(event="reading", message="Reading repo", data={"step": 1})
        context.report_progress(event="summarizing", message="Summarizing repo", data={"step": 2})
        return ToolExecutionPayload(
            content=f"echo:{parsed_input.text}",
            output_payload={"echo": parsed_input.text},
            context_modifier={"cache": "updated"},
        )


class InvalidEchoTool(RuntimeTool):
    name = "invalid_echo"
    description = "Invalid echo"
    input_model = EchoInput

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        raise AssertionError("should not execute")


class CrashingEchoTool(RuntimeTool):
    name = "crash_echo"
    description = "Crash echo"
    input_model = EchoInput

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        raise RuntimeError("boom")


def test_tool_orchestrator_persists_progress_events_and_context_modifier_metadata():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry([ProgressEchoTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry)

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[ToolCallRequest(id="tool-1", name="progress_echo", input={"text": "alpha"})],
        )
    )
    snapshot = store.load_session_snapshot(session_id)

    assert records[0].status == AuditToolCallStatus.COMPLETED.value
    assert records[0].result.context_modifier == {"cache": "updated"}
    assert records[0].result.metadata["progress_event_count"] == 4
    assert records[0].lifecycle["context_modifier"] == {"cache": "updated"}
    progress_checkpoints = [checkpoint.state_payload for checkpoint in snapshot.checkpoints if checkpoint.state_payload.get("kind") == "runtime_tool_progress"]
    assert [item["event"] for item in progress_checkpoints] == ["tool_start", "reading", "summarizing", "tool_complete"]
    assert snapshot.tool_calls[0].output_payload["context_modifier"] == {"cache": "updated"}


def test_tool_orchestrator_formats_validation_errors_with_error_kind_metadata():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry([InvalidEchoTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry)

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[ToolCallRequest(id="tool-1", name="invalid_echo", input={})],
        )
    )

    assert records[0].status == AuditToolCallStatus.INVALID.value
    assert records[0].result.metadata["error_kind"] == "validation_error"
    assert "text" in records[0].error_message


def test_tool_orchestrator_classifies_execution_errors_and_permission_metadata():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry([DeniedWriteTool(), CrashingEchoTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry)

    denied_record, failed_record = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[
                ToolCallRequest(id="tool-1", name="write_file", input={"text": "secret"}),
                ToolCallRequest(id="tool-2", name="crash_echo", input={"text": "boom"}),
            ],
        )
    )

    assert denied_record.result.metadata["error_kind"] == "permission_denied"
    assert denied_record.lifecycle["permission_decision"]["phase"] == "tool"
    assert failed_record.result.metadata["error_kind"] == "execution_error"
    assert failed_record.lifecycle["progress_events"][0]["event"] == "tool_start"
