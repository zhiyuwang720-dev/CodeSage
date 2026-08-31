from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.audit_session import AuditMemoryKind
from app.services.review_runtime.adapters.finding import FindingRuntimeAdapter
from app.services.review_runtime.models import RuntimeMemoryBundle, RuntimeMemoryRecord, RuntimeMessageRole, RuntimeStopReason, TranscriptItem
from app.services.review_runtime.query_state import QueryLoopState
from app.services.review_runtime.session_store import AuditSessionStore


class FakeRunner:
    def __init__(self):
        self.calls = []

    async def run_once(self, *, session_id: str, model_name: str):
        self.calls.append({"session_id": session_id, "model_name": model_name})
        return {"stop_reason": RuntimeStopReason.COMPLETED.value}


class FakeSkillCatalog:
    async def preload(self, *, user_id, agent_type, context):
        return type(
            "SkillCatalogSnapshot",
            (),
            {
                "available_skills": [
                    {
                        "id": "code-audit-finding",
                        "slug": "code-audit-finding",
                        "name": "Code Audit Finding",
                        "description": "primary skill",
                        "source_type": "bundled",
                        "paths": {
                            "skill_file_path": "/app/skill_library/code-audit-finding/SKILL.md",
                        },
                    }
                ],
                "matched_skills": [
                    {
                        "id": "code-audit-finding",
                        "slug": "code-audit-finding",
                        "name": "Code Audit Finding",
                        "description": "primary skill",
                        "source_type": "bundled",
                        "paths": {
                            "skill_file_path": "/app/skill_library/code-audit-finding/SKILL.md",
                        },
                    }
                ],
                "prompt": "skills prompt",
                "route_message": "skills route message",
                "route_plan": {"primary_skill": "code-audit-finding"},
            },
        )()


class FakeSkillService:
    @staticmethod
    def get_skill_entry(user_id, skill_ref, agent_type=None):
        return None

    @staticmethod
    async def get_skill_body(user_id, skill_ref, agent_type=None):
        return {
            "skill": "Code Audit Finding",
            "slug": skill_ref,
            "skill_file": "/app/skill_library/code-audit-finding/SKILL.md",
            "content": "FULL SKILL BODY\nRequired startup protocol.",
        }

    @staticmethod
    async def list_skill_resources(user_id, skill_ref, resource_name="", agent_type=None):
        return {"items": []}

    @staticmethod
    async def get_skill_resource(user_id, skill_ref, resource_name, agent_type=None):
        return {"content": "resource"}


class FakeMemoryManager:
    async def preload(self, *, agent_type, system_prompt, recon_payload, user_message, skill_context):
        return RuntimeMemoryBundle(
            instructions=[
                RuntimeMemoryRecord(
                    memory_kind=AuditMemoryKind.INSTRUCTION.value,
                    title="Rule set: baseline",
                    source_type="audit_rule_set",
                    source_ref="ruleset-1",
                    content="Always verify authorization boundaries.",
                )
            ],
            recalls=[
                RuntimeMemoryRecord(
                    memory_kind=AuditMemoryKind.RECALL.value,
                    title="python.md",
                    source_type="skill_reference",
                    source_ref="references/checklists/python.md",
                    content="Python checklist",
                    relevance_score=41,
                )
            ],
        )


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_finding_runtime_adapter_preserves_prompt_recon_skill_route_and_memories():
    store = build_store()
    runner = FakeRunner()
    adapter = FindingRuntimeAdapter(
        session_store=store,
        runner=runner,
        skill_catalog=FakeSkillCatalog(),
        memory_manager=FakeMemoryManager(),
    )

    result = asyncio.run(
        adapter.run(
            project_id="project-1",
            task_id="task-1",
            system_prompt="keep this prompt",
            recon_payload={"repo": "demo", "entry_points": ["/api"]},
            user_message="audit this target",
            model_name="gpt-test",
        )
    )

    snapshot = store.load_session_snapshot(result["session_id"])

    assert snapshot.skills[0].skill_ref == "code-audit-finding"
    assert snapshot.memories[0].memory_kind == AuditMemoryKind.INSTRUCTION.value
    assert snapshot.memories[1].source_ref == "references/checklists/python.md"
    assert "keep this prompt" in snapshot.session.system_prompt
    assert "skills prompt" in snapshot.session.system_prompt
    assert "skills route message" in snapshot.session.system_prompt
    assert "Always verify authorization boundaries." in snapshot.session.system_prompt
    assert "Python checklist" in snapshot.session.system_prompt
    assert len(snapshot.messages) == 1
    assert snapshot.messages[0].role == "user"
    assert snapshot.messages[0].content == "audit this target"
    assert result["memory_counts"] == {"instruction": 1, "recall": 1}
    assert runner.calls == [{"session_id": result["session_id"], "model_name": "gpt-test"}]


def test_finding_runtime_adapter_injects_explicit_skill_mentions_with_audited_invocation():
    store = build_store()
    adapter = FindingRuntimeAdapter(
        session_store=store,
        runner=FakeRunner(),
        skill_catalog=FakeSkillCatalog(),
        memory_manager=FakeMemoryManager(),
        skill_service=FakeSkillService(),
    )

    result = asyncio.run(
        adapter.run(
            project_id="project-1",
            task_id="task-1",
            system_prompt="Read $code-audit-finding before working.",
            recon_payload={"repo": "demo"},
            user_message="audit this target",
            model_name="gpt-test",
        )
    )

    snapshot = store.load_session_snapshot(result["session_id"])
    invocations = store.list_skill_invocations(result["session_id"])

    assert "<explicit_skill>" in snapshot.session.system_prompt
    assert "<skill_ref>code-audit-finding</skill_ref>" in snapshot.session.system_prompt
    assert "FULL SKILL BODY" in snapshot.session.system_prompt
    assert "显式技能加载提醒" in snapshot.session.system_prompt
    assert "Primary skill bootstrap" not in snapshot.session.system_prompt
    assert len(invocations) == 1
    assert invocations[0].skill_ref == "code-audit-finding"
    assert invocations[0].input_payload["invocation_source"] == "explicit_mention"
    assert invocations[0].input_payload["mention_source"] == "system"


def test_finding_runtime_adapter_defaults_user_message_when_not_provided():
    store = build_store()
    adapter = FindingRuntimeAdapter(
        session_store=store,
        runner=FakeRunner(),
        skill_catalog=FakeSkillCatalog(),
        memory_manager=FakeMemoryManager(),
    )

    result = asyncio.run(
        adapter.run(
            project_id="project-1",
            task_id="task-1",
            system_prompt="prompt",
            recon_payload={"repo": "demo"},
        )
    )

    snapshot = store.load_session_snapshot(result["session_id"])

    assert snapshot.messages[-1].content == "Continue the audit with the current Finding objective."


def test_refresh_session_context_rehydrates_query_loop_state_with_latest_user_message():
    store = build_store()
    adapter = FindingRuntimeAdapter(
        session_store=store,
        runner=FakeRunner(),
        skill_catalog=FakeSkillCatalog(),
        memory_manager=FakeMemoryManager(),
    )

    result = asyncio.run(
        adapter.run(
            project_id="project-1",
            task_id="task-1",
            system_prompt="prompt",
            recon_payload={"repo": "demo"},
            user_message="first turn",
        )
    )

    session_id = result["session_id"]
    store.save_query_loop_state(
        session_id,
        QueryLoopState(
            messages=[
                TranscriptItem(
                    role=RuntimeMessageRole.USER,
                    content="first turn",
                )
            ],
            turn_count=2,
        ),
    )
    store.append_message(
        session_id,
        TranscriptItem(
            role=RuntimeMessageRole.USER,
            content="follow-up question",
        ),
    )

    asyncio.run(adapter.refresh_session_context(session_id=session_id))

    refreshed_state = store.load_query_loop_state(session_id)

    assert refreshed_state.messages[-1].role == RuntimeMessageRole.USER
    assert refreshed_state.messages[-1].content == "follow-up question"
    assert [message.content for message in refreshed_state.messages if message.role == RuntimeMessageRole.USER] == [
        "first turn",
        "follow-up question",
    ]
