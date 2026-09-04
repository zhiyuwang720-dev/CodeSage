from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.audit_session import AuditCheckpointType, AuditToolCallStatus
from app.services.contracts.models import ToolCallRequest, ToolExecutionPayload
from app.services.agent.tools.todo_runtime_tool import TodoWriteRuntimeTool
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.runtime_core.tool_runtime import (
    RuntimeTool,
    ToolExecutionContext,
    ToolOrchestrator,
    ToolRegistry,
)
from app.services.runtime_core.runtime_tool_registry import CanonicalWriteTool


class EchoInput(BaseModel):
    text: str


class ReadTool(RuntimeTool):
    name = "Read"
    description = "Read text"
    input_model = EchoInput

    def is_concurrency_safe(self, parsed_input: EchoInput) -> bool:
        return True

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        return ToolExecutionPayload(
            content=f"read:{parsed_input.text}",
            output_payload={"echo": parsed_input.text, "agent": context.agent_type},
        )


class WriteTool(RuntimeTool):
    name = "Write"
    description = "Write text"
    input_model = EchoInput

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        return ToolExecutionPayload(
            content=f"write:{parsed_input.text}",
            output_payload={"echo": parsed_input.text},
        )


class SkillTool(RuntimeTool):
    name = "Skill"
    description = "Skill tool"
    input_model = EchoInput

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        return ToolExecutionPayload(
            content="skill invoked",
            output_payload={"skill": parsed_input.text},
        )


class SlowTool(RuntimeTool):
    name = "Slow"
    description = "Slow tool"
    input_model = EchoInput

    async def execute(self, parsed_input: EchoInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        await asyncio.sleep(float(parsed_input.text))
        return ToolExecutionPayload(content="finished")


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_tool_orchestrator_times_out_slow_runtime_tools():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    registry = ToolRegistry([SlowTool()])
    orchestrator = ToolOrchestrator(
        session_store=store,
        tool_registry=registry,
        agent_type="finding",
        default_tool_timeout_seconds=0.01,
    )

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[ToolCallRequest(id="tool-1", name="Slow", input={"text": "1"})],
        )
    )

    snapshot = store.load_session_snapshot(session_id)

    assert records[0].status == AuditToolCallStatus.FAILED.value
    assert records[0].result.is_error is True
    assert records[0].result.metadata["error_kind"] == "timeout_error"
    assert "path/glob/pattern" in records[0].error_message
    assert snapshot.tool_calls[0].status == AuditToolCallStatus.FAILED.value
    assert snapshot.tool_calls[0].error_message == records[0].error_message


def build_workspace_temp_dir() -> Path:
    workspace_root = Path(__file__).resolve().parents[3]
    temp_root = workspace_root / ".pytest-runtime-write"
    temp_root.mkdir(parents=True, exist_ok=True)
    project_root = temp_root / f"runtime-write-{uuid.uuid4().hex[:8]}"
    project_root.mkdir(parents=True, exist_ok=True)
    return project_root


def seed_skill_runtime_state(store: AuditSessionStore, session_id: str) -> None:
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.record_skill_contract(
        agent_type="finding",
        skill_ref="code-audit-finding",
        contract={
            "allowed_tools": ["Read"],
            "hooks": {
                "PreToolUse": [{"matcher": "*", "hooks": ["log-pre"]}],
                "PostToolUse": [{"matcher": "*", "hooks": ["log-post"]}],
                "PermissionDenied": [{"matcher": "*", "hooks": ["deny-log"]}],
            },
        },
    )
    store.replace_runtime_state(session_id, runtime_state)


def test_shared_tool_runtime_enforces_allowed_tools_and_records_denial_hooks():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    seed_skill_runtime_state(store, session_id)
    registry = ToolRegistry([WriteTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[ToolCallRequest(id="tool-1", name="Write", input={"text": "secret"})],
        )
    )

    snapshot = store.load_session_snapshot(session_id)

    assert records[0].status == AuditToolCallStatus.DENIED.value
    assert "allowed_tools" in (records[0].error_message or "")
    assert snapshot.tool_calls[0].status == AuditToolCallStatus.DENIED.value
    assert snapshot.checkpoints[0].checkpoint_type == AuditCheckpointType.AUTO.value
    assert snapshot.checkpoints[0].state_payload["event"] == "PermissionDenied"
    assert snapshot.checkpoints[0].state_payload["tool_name"] == "Write"


def test_shared_tool_runtime_emits_pre_and_post_hooks_for_allowed_tool():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    seed_skill_runtime_state(store, session_id)
    registry = ToolRegistry([ReadTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[ToolCallRequest(id="tool-1", name="Read", input={"text": "alpha"})],
        )
    )

    snapshot = store.load_session_snapshot(session_id)
    hook_events = [
        checkpoint.state_payload["event"]
        for checkpoint in snapshot.checkpoints
        if checkpoint.state_payload.get("kind") == "runtime_hook"
    ]

    assert records[0].status == AuditToolCallStatus.COMPLETED.value
    assert records[0].result.output_payload == {"echo": "alpha", "agent": "finding"}
    assert hook_events == ["PreToolUse", "PostToolUse"]


class ScopedReadInput(BaseModel):
    text: str
    scope: str


class ScopedConcurrentTool(RuntimeTool):
    name = "ScopedRead"
    description = "Scoped concurrent tool"
    input_model = ScopedReadInput

    def __init__(self, events: list[tuple[str, str]]):
        self._events = events

    def is_concurrency_safe(self, parsed_input: ScopedReadInput) -> bool:
        return True

    def concurrency_key(self, parsed_input: ScopedReadInput) -> str | None:
        return parsed_input.scope

    async def execute(self, parsed_input: ScopedReadInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        self._events.append(("start", f"{parsed_input.scope}:{parsed_input.text}"))
        await asyncio.sleep(0.01)
        self._events.append(("end", f"{parsed_input.scope}:{parsed_input.text}"))
        return ToolExecutionPayload(
            content=f"scoped:{parsed_input.text}",
            output_payload={"scope": parsed_input.scope, "echo": parsed_input.text},
        )


def test_shared_tool_runtime_serializes_conflicting_concurrency_keys():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    events: list[tuple[str, str]] = []
    registry = ToolRegistry([ScopedConcurrentTool(events)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[
                ToolCallRequest(id="tool-1", name="ScopedRead", input={"text": "alpha", "scope": "fs"}),
                ToolCallRequest(id="tool-2", name="ScopedRead", input={"text": "beta", "scope": "fs"}),
                ToolCallRequest(id="tool-3", name="ScopedRead", input={"text": "gamma", "scope": "net"}),
            ],
        )
    )

    assert [record.status for record in records] == [AuditToolCallStatus.COMPLETED.value] * 3
    assert events[0] == ("start", "fs:alpha")
    assert events[1] == ("end", "fs:alpha")
    assert events[2] == ("start", "fs:beta")
    assert events[3] == ("start", "net:gamma")


def test_shared_tool_runtime_converts_ask_permission_rules_into_denied_tool_records():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["permission_rules"] = {
        "finding": {
            "Write": {"mode": "ask", "reason": "Need human approval before write actions."}
        }
    }
    store.replace_runtime_state(session_id, runtime_state)
    registry = ToolRegistry([WriteTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[ToolCallRequest(id="tool-1", name="Write", input={"text": "secret"})],
        )
    )

    snapshot = store.load_session_snapshot(session_id)

    assert records[0].status == AuditToolCallStatus.DENIED.value
    assert "approval" in (records[0].error_message or "").lower()
    assert snapshot.tool_calls[0].status == AuditToolCallStatus.DENIED.value
    assert snapshot.checkpoints[0].state_payload["event"] == "PermissionDenied"
    assert snapshot.checkpoints[0].state_payload["source"] == "permission_rule"


def test_shared_tool_runtime_keeps_system_interaction_tools_available_under_skill_permissions():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    seed_skill_runtime_state(store, session_id)
    registry = ToolRegistry([TodoWriteRuntimeTool(store)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[
                ToolCallRequest(
                    id="tool-1",
                    name="TodoWrite",
                    input={"title": "Capture exploit chain", "details": "Confirm auth bypass path"},
                )
            ],
        )
    )

    runtime_state = store.load_runtime_state(session_id)
    agent_state = runtime_state.agent_states["finding"]

    assert records[0].status == AuditToolCallStatus.COMPLETED.value
    assert agent_state.pending_todos[0]["title"] == "Capture exploit chain"
    assert agent_state.pending_todos[0]["details"] == "Confirm auth bypass path"


def test_shared_tool_runtime_keeps_skill_tool_available_under_skill_permissions():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    seed_skill_runtime_state(store, session_id)
    registry = ToolRegistry([SkillTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")

    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            tool_calls=[ToolCallRequest(id="tool-1", name="Skill", input={"text": "code-audit-finding"})],
        )
    )

    assert records[0].status == AuditToolCallStatus.COMPLETED.value
    assert records[0].result.output_payload == {"skill": "code-audit-finding"}


def test_canonical_write_tool_writes_artifacts_under_managed_output_dir():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    project_root = build_workspace_temp_dir()
    registry = ToolRegistry([CanonicalWriteTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    try:
        records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=turn_id,
                session=type(
                    "SessionRef",
                    (),
                    {
                        "recon_payload": {
                            "project_info": {
                                "workspace_root": str(project_root),
                                "name": "demo-project",
                            }
                        }
                    },
                )(),
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="Write",
                        input={"path": ".auditai/outputs/report.md", "content": "# report", "overwrite": True},
                    )
                ],
            )
        )
        written_file = project_root / ".auditai" / "outputs" / "report.md"
        snapshot = store.load_session_snapshot(session_id)

        assert records[0].status == AuditToolCallStatus.COMPLETED.value
        assert written_file.read_text(encoding="utf-8") == "# report"
        assert snapshot.tool_calls[0].output_payload["artifact_type"] == "managed_output"
        assert snapshot.tool_calls[0].output_payload["resolved_path"] == str(written_file.resolve())
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_canonical_write_tool_treats_any_dot_auditai_path_as_managed_output():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    project_root = build_workspace_temp_dir()
    registry = ToolRegistry([CanonicalWriteTool()])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    try:
        records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=turn_id,
                session=type(
                    "SessionRef",
                    (),
                    {
                        "recon_payload": {
                            "project_info": {
                                "workspace_root": str(project_root),
                                "name": "demo-project",
                            }
                        }
                    },
                )(),
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="Write",
                        input={"path": ".auditai/reports/report.md", "content": "# report", "overwrite": True},
                    )
                ],
            )
        )
        written_file = project_root / ".auditai" / "reports" / "report.md"
        snapshot = store.load_session_snapshot(session_id)

        assert records[0].status == AuditToolCallStatus.COMPLETED.value
        assert written_file.read_text(encoding="utf-8") == "# report"
        assert snapshot.tool_calls[0].output_payload["artifact_type"] == "managed_output"
        assert snapshot.tool_calls[0].output_payload["resolved_path"] == str(written_file.resolve())
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_canonical_write_tool_prefers_configured_project_root_over_session_payload(tmp_path):
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    project_root = tmp_path / "current-project"
    stale_root = tmp_path / "stale-project"
    project_root.mkdir()
    stale_root.mkdir()
    registry = ToolRegistry([CanonicalWriteTool(project_root=str(project_root))])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    records = asyncio.run(
        orchestrator.execute_tool_calls(
            session_id=session_id,
            turn_id=turn_id,
            session=type(
                "SessionRef",
                (),
                {
                    "recon_payload": {
                        "project_info": {
                            "workspace_root": str(stale_root),
                            "name": "demo-project",
                        }
                    }
                },
            )(),
            recon_payload={},
            tool_calls=[
                ToolCallRequest(
                    id="tool-1",
                    name="Write",
                    input={"path": ".auditai/tasks/task-1/report.md", "content": "# report", "overwrite": True},
                )
            ],
        )
    )
    written_file = project_root / ".auditai" / "tasks" / "task-1" / "report.md"
    stale_file = stale_root / ".auditai" / "tasks" / "task-1" / "report.md"

    assert records[0].status == AuditToolCallStatus.COMPLETED.value
    assert written_file.read_text(encoding="utf-8") == "# report"
    assert not stale_file.exists()
    assert records[0].result.output_payload["resolved_path"] == str(written_file.resolve())


def test_canonical_write_tool_requires_approval_for_source_tree_writes():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    store.replace_runtime_state(session_id, runtime_state)
    project_root = build_workspace_temp_dir()
    registry = ToolRegistry([CanonicalWriteTool(session_store=store)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    try:
        records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=turn_id,
                session=type(
                    "SessionRef",
                    (),
                    {
                        "recon_payload": {
                            "project_info": {
                                "workspace_root": str(project_root),
                                "name": "demo-project",
                            }
                        }
                    },
                )(),
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="Write",
                        input={"path": "src/app.py", "content": "print('mutate')", "overwrite": True},
                    )
                ],
            )
        )
        snapshot = store.load_session_snapshot(session_id)

        assert records[0].status == AuditToolCallStatus.DENIED.value
        assert "批准" in (records[0].error_message or "")
        assert snapshot.tool_calls[0].output_payload["permission_mode"] == "ask"
        assert snapshot.tool_calls[0].output_payload["guardrail_code"] == "source_write_requires_approval"
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_canonical_write_tool_allows_source_tree_write_when_guardrails_are_disabled():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    project_root = build_workspace_temp_dir()
    registry = ToolRegistry([CanonicalWriteTool(session_store=store)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    try:
        records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=turn_id,
                session=type(
                    "SessionRef",
                    (),
                    {
                        "recon_payload": {
                            "project_info": {
                                "workspace_root": str(project_root),
                                "name": "demo-project",
                            }
                        }
                    },
                )(),
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="Write",
                        input={"path": "src/app.py", "content": "print('mutate')", "overwrite": True},
                    )
                ],
            )
        )
        written_file = project_root / "src" / "app.py"

        assert records[0].status == AuditToolCallStatus.COMPLETED.value
        assert written_file.read_text(encoding="utf-8") == "print('mutate')"
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_canonical_write_tool_allows_session_approved_source_tree_write():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    runtime_state.metadata["write_approvals"] = [
        {
            "path": "src/app.py",
            "guardrail_code": "source_write_requires_approval",
        }
    ]
    store.replace_runtime_state(session_id, runtime_state)
    project_root = build_workspace_temp_dir()
    registry = ToolRegistry([CanonicalWriteTool(session_store=store)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    try:
        records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=turn_id,
                session=type(
                    "SessionRef",
                    (),
                    {
                        "recon_payload": {
                            "project_info": {
                                "workspace_root": str(project_root),
                                "name": "demo-project",
                            }
                        }
                    },
                )(),
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="Write",
                        input={"path": "src/app.py", "content": "print('approved')", "overwrite": True},
                    )
                ],
            )
        )
        written_file = project_root / "src" / "app.py"

        assert records[0].status == AuditToolCallStatus.COMPLETED.value
        assert written_file.read_text(encoding="utf-8") == "print('approved')"
        assert records[0].result.output_payload["resolved_path"] == str(written_file.resolve())
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_canonical_write_tool_requires_approval_before_overwriting_existing_artifact_when_guardrails_enabled():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    store.replace_runtime_state(session_id, runtime_state)
    project_root = build_workspace_temp_dir()
    existing_file = project_root / ".auditai" / "reports" / "report.md"
    existing_file.parent.mkdir(parents=True, exist_ok=True)
    existing_file.write_text("old", encoding="utf-8")
    registry = ToolRegistry([CanonicalWriteTool(session_store=store)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    try:
        records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=turn_id,
                session=type(
                    "SessionRef",
                    (),
                    {
                        "recon_payload": {
                            "project_info": {
                                "workspace_root": str(project_root),
                                "name": "demo-project",
                            }
                        }
                    },
                )(),
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="Write",
                        input={"path": ".auditai/reports/report.md", "content": "new", "overwrite": True},
                    )
                ],
            )
        )
        snapshot = store.load_session_snapshot(session_id)

        assert records[0].status == AuditToolCallStatus.DENIED.value
        assert snapshot.tool_calls[0].output_payload["permission_mode"] == "ask"
        assert snapshot.tool_calls[0].output_payload["guardrail_code"] == "overwrite_existing_requires_approval"
        assert existing_file.read_text(encoding="utf-8") == "old"
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_canonical_write_tool_consumes_single_use_source_write_approval_after_first_execution():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    runtime_state.metadata["write_approvals"] = [
        {
            "path": "src/app.py",
            "guardrail_code": "source_write_requires_approval",
            "scope": "single_use",
        }
    ]
    store.replace_runtime_state(session_id, runtime_state)
    project_root = build_workspace_temp_dir()
    registry = ToolRegistry([CanonicalWriteTool(session_store=store)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    session_ref = type(
        "SessionRef",
        (),
        {
            "recon_payload": {
                "project_info": {
                    "workspace_root": str(project_root),
                    "name": "demo-project",
                }
            }
        },
    )()
    try:
        first_turn_id = store.open_turn(session_id, model_name="gpt-test")
        first_records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=first_turn_id,
                session=session_ref,
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="Write",
                        input={"path": "src/app.py", "content": "print('approved once')", "overwrite": True},
                    )
                ],
            )
        )
        persisted_state = store.load_runtime_state(session_id)
        second_turn_id = store.open_turn(session_id, model_name="gpt-test")
        second_records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=second_turn_id,
                session=session_ref,
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-2",
                        name="Write",
                        input={"path": "src/app.py", "content": "print('approved twice')", "overwrite": True},
                    )
                ],
            )
        )
        second_snapshot = store.load_session_snapshot(session_id)

        assert first_records[0].status == AuditToolCallStatus.COMPLETED.value
        assert persisted_state.metadata["write_approvals"][0]["scope"] == "single_use"
        assert persisted_state.metadata["write_approvals"][0].get("consumed_at")
        assert second_records[0].status == AuditToolCallStatus.DENIED.value
        assert second_snapshot.tool_calls[-1].output_payload["guardrail_code"] == "source_write_requires_approval"
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def test_canonical_write_tool_keeps_session_scope_source_write_approval_reusable():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    runtime_state = store.load_runtime_state(session_id)
    runtime_state.metadata["guardrails"] = {"enabled": True}
    runtime_state.metadata["write_approvals"] = [
        {
            "path": "src/app.py",
            "guardrail_code": "source_write_requires_approval",
            "scope": "session",
        }
    ]
    store.replace_runtime_state(session_id, runtime_state)
    project_root = build_workspace_temp_dir()
    registry = ToolRegistry([CanonicalWriteTool(session_store=store)])
    orchestrator = ToolOrchestrator(session_store=store, tool_registry=registry, agent_type="finding")
    session_ref = type(
        "SessionRef",
        (),
        {
            "recon_payload": {
                "project_info": {
                    "workspace_root": str(project_root),
                    "name": "demo-project",
                }
            }
        },
    )()
    try:
        first_turn_id = store.open_turn(session_id, model_name="gpt-test")
        second_turn_id = store.open_turn(session_id, model_name="gpt-test")
        first_records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=first_turn_id,
                session=session_ref,
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="Write",
                        input={"path": "src/app.py", "content": "print('session one')", "overwrite": True},
                    )
                ],
            )
        )
        second_records = asyncio.run(
            orchestrator.execute_tool_calls(
                session_id=session_id,
                turn_id=second_turn_id,
                session=session_ref,
                recon_payload={"project_info": {"workspace_root": str(project_root), "name": "demo-project"}},
                tool_calls=[
                    ToolCallRequest(
                        id="tool-2",
                        name="Write",
                        input={"path": "src/app.py", "content": "print('session two')", "overwrite": True},
                    )
                ],
            )
        )
        persisted_state = store.load_runtime_state(session_id)
        written_file = project_root / "src" / "app.py"

        assert first_records[0].status == AuditToolCallStatus.COMPLETED.value
        assert second_records[0].status == AuditToolCallStatus.COMPLETED.value
        assert persisted_state.metadata["write_approvals"][0]["scope"] == "session"
        assert persisted_state.metadata["write_approvals"][0].get("consumed_at") is None
        assert written_file.read_text(encoding="utf-8") == "print('session two')"
    finally:
        shutil.rmtree(project_root, ignore_errors=True)
