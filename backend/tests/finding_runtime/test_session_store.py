from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.audit_session import AuditCheckpointType, AuditMemoryKind, AuditSkillInvocationStatus, AuditToolCallStatus
from app.services.review_runtime.models import RuntimeMemoryRecord, RuntimeMessageRole, RuntimeSessionState, TranscriptItem
from app.services.review_runtime.session_store import AuditSessionStore


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_create_session_persists_runtime_stack_and_state():
    store = build_store()

    session_id = store.create_session(
        project_id="project-1",
        task_id="task-1",
        runtime_stack="runtime",
        system_prompt="prompt",
        recon_payload={"repo": "demo"},
    )

    snapshot = store.load_session_snapshot(session_id)

    assert snapshot.session.id == session_id
    assert snapshot.session.project_id == "project-1"
    assert snapshot.session.task_id == "task-1"
    assert snapshot.session.runtime_stack == "runtime"
    assert snapshot.session.state == RuntimeSessionState.PENDING.value


def test_append_message_persists_transcript_sequence_in_order():
    store = build_store()
    session_id = store.create_session(project_id="project-1")

    first_id = store.append_message(
        session_id,
        TranscriptItem(role=RuntimeMessageRole.SYSTEM, content="system message"),
    )
    second_id = store.append_message(
        session_id,
        TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="assistant reply"),
    )

    snapshot = store.load_session_snapshot(session_id)

    assert [message.id for message in snapshot.messages] == [first_id, second_id]
    assert [message.sequence for message in snapshot.messages] == [1, 2]
    assert [message.role for message in snapshot.messages] == ["system", "assistant"]


def test_message_writes_recursively_escape_nul_characters():
    store = build_store()
    session_id = store.create_session(project_id="project-1")

    item = TranscriptItem(
        role=RuntimeMessageRole.TOOL_RESULT,
        content="source contains \x00 byte",
        name="Read\x00Tool",
        metadata={"nested": {"value": "meta\x00data"}},
        payload={"output": ["first", {"value": "payload\x00data"}]},
    )
    message_id = store.append_message(session_id, item)
    store.update_message_content(message_id, content="updated\x00content")
    store.update_message_payload(
        message_id,
        payload={"extra": {"value": "extra\x00data"}},
    )

    message = store.get_message(message_id)

    assert message is not None
    assert item.content == "source contains \\x00 byte"
    assert item.metadata == {"nested": {"value": "meta\\x00data"}}
    assert item.payload == {"output": ["first", {"value": "payload\\x00data"}]}
    assert message.content == "updated\\x00content"
    assert message.name == "Read\\x00Tool"
    assert message.message_metadata == {"nested": {"value": "meta\\x00data"}}
    assert message.payload == {
        "output": ["first", {"value": "payload\\x00data"}],
        "extra": {"value": "extra\\x00data"},
    }


def test_open_turn_checkpoint_tool_call_skill_invocation_and_memories_round_trip():
    store = build_store()
    session_id = store.create_session(project_id="project-1")

    store.replace_skills(
        session_id,
        [{"slug": "code-audit-finding", "name": "Code Audit", "description": "primary skill", "source_type": "bundled"}],
        matched_skill_refs={"code-audit-finding"},
    )
    store.replace_memories(
        session_id,
        [
            RuntimeMemoryRecord(
                memory_kind=AuditMemoryKind.INSTRUCTION.value,
                title="Rule set: baseline",
                source_type="audit_rule_set",
                source_ref="ruleset-1",
                content="Always verify authz.",
            ),
            RuntimeMemoryRecord(
                memory_kind=AuditMemoryKind.RECALL.value,
                title="python.md",
                source_type="skill_reference",
                source_ref="references/checklists/python.md",
                content="Python checklist",
                relevance_score=52,
            ),
        ],
    )
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    tool_call_id = store.start_tool_call(
        session_id=session_id,
        turn_id=turn_id,
        tool_use_id="tool-use-1",
        tool_name="echo",
        input_payload={"text": "demo"},
        is_concurrency_safe=True,
    )
    store.complete_tool_call(
        tool_call_id,
        status=AuditToolCallStatus.COMPLETED.value,
        output_payload={"text": "demo"},
        duration_ms=12,
    )
    invocation_id = store.start_skill_invocation(
        session_id=session_id,
        turn_id=turn_id,
        skill_ref="code-audit-finding",
        input_payload={"action": "body"},
    )
    store.complete_skill_invocation(
        invocation_id,
        status=AuditSkillInvocationStatus.COMPLETED.value,
        output_payload={"body": "Skill body"},
    )
    checkpoint_id = store.create_checkpoint(
        session_id=session_id,
        turn_id=turn_id,
        checkpoint_type=AuditCheckpointType.AUTO,
        state_payload={"cursor": 1},
    )
    store.close_turn(turn_id, status="completed")
    store.update_session_state(session_id, RuntimeSessionState.RUNNING)

    snapshot = store.load_session_snapshot(session_id)

    assert snapshot.session.state == RuntimeSessionState.RUNNING.value
    assert len(snapshot.turns) == 1
    assert snapshot.turns[0].id == turn_id
    assert snapshot.turns[0].status == "completed"
    assert len(snapshot.skills) == 1
    assert snapshot.skills[0].skill_ref == "code-audit-finding"
    assert snapshot.skills[0].matched is True
    assert len(snapshot.memories) == 2
    assert snapshot.memories[0].memory_kind == AuditMemoryKind.INSTRUCTION.value
    assert snapshot.memories[1].relevance_score == 52
    assert len(snapshot.tool_calls) == 1
    assert snapshot.tool_calls[0].id == tool_call_id
    assert snapshot.tool_calls[0].status == AuditToolCallStatus.COMPLETED.value
    assert len(snapshot.skill_invocations) == 1
    assert snapshot.skill_invocations[0].id == invocation_id
    assert snapshot.skill_invocations[0].status == AuditSkillInvocationStatus.COMPLETED.value
    assert snapshot.skill_invocations[0].output_payload == {"body": "Skill body"}
    assert len(snapshot.checkpoints) == 1
    assert snapshot.checkpoints[0].id == checkpoint_id


def test_create_handoff_persists_record_and_appends_transcript_message():
    store = build_store()
    session_id = store.create_session(project_id="project-1", task_id="task-1", runtime_stack="runtime")

    handoff_id = store.create_handoff(
        session_id=session_id,
        target="verification",
        status="pending",
        payload={
            "from_agent": "finding",
            "to_agent": "verification",
            "summary": "Need dynamic proof for exploit chain.",
        },
    )

    snapshot = store.load_session_snapshot(session_id)
    handoffs = store.list_handoffs(session_id)

    assert handoff_id
    assert len(handoffs) == 1
    assert handoffs[0].target == "verification"
    assert len(snapshot.messages) == 1
    assert snapshot.messages[0].role == "handoff"
    assert snapshot.messages[0].message_metadata["kind"] == "handoff"
    assert snapshot.messages[0].payload["target"] == "verification"
