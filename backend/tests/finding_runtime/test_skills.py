from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.audit_session import AuditSkillInvocationStatus
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.review_runtime.skills import RuntimeSkillCatalog, RuntimeSkillTool
from app.services.review_runtime.tooling import ToolExecutionContext


class FakeSkillService:
    @staticmethod
    async def resolve_agent_skills(user_id, agent_type, context):
        assert agent_type == "finding"
        return {
            "metadata": [
                {
                    "id": "code-audit-finding",
                    "slug": "code-audit-finding",
                    "name": "Code Audit Finding",
                    "description": "Primary finding skill",
                    "source_type": "bundled",
                }
            ],
            "matched": [
                {
                    "id": "code-audit-finding",
                    "slug": "code-audit-finding",
                    "name": "Code Audit Finding",
                    "description": "Primary finding skill",
                    "source_type": "bundled",
                }
            ],
            "prompt": "catalog prompt",
            "route_plan": {"primary_skill": "code-audit-finding", "secondary_skills": []},
        }

    @staticmethod
    def build_skill_briefing(skill_context):
        return f"Skills runtime catalog:\n{skill_context['prompt']}"

    @staticmethod
    async def get_skill_body(user_id, skill_ref, agent_type=None):
        return {"skill": skill_ref, "content": "body"}

    @staticmethod
    async def list_skill_resources(user_id, skill_ref, resource_name="", agent_type=None):
        return {"skill": skill_ref, "mode": "list", "resource_name": resource_name, "items": []}

    @staticmethod
    async def get_skill_resource(user_id, skill_ref, resource_name, agent_type=None):
        return {"skill": skill_ref, "resource": resource_name, "content": "resource body"}


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_runtime_skill_catalog_builds_route_snapshot():
    catalog = RuntimeSkillCatalog(skill_service=FakeSkillService())

    snapshot = asyncio.run(
        catalog.preload(
            user_id=None,
            agent_type="finding",
            context={"recon_data": {"summary": "auth bug"}, "task": "audit auth flow", "config": {}},
        )
    )

    assert snapshot.available_skills[0]["slug"] == "code-audit-finding"
    assert snapshot.matched_skills[0]["slug"] == "code-audit-finding"
    assert snapshot.prompt.startswith("Skills runtime catalog")
    assert snapshot.route_plan["primary_skill"] == "code-audit-finding"


def test_runtime_skill_tool_persists_skill_invocation_and_runtime_state():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    tool = RuntimeSkillTool(session_store=store, skill_service=FakeSkillService())

    payload = asyncio.run(
        tool.execute(
            tool.input_model(skill_ref="code-audit-finding", action="body"),
            ToolExecutionContext(
                session_id=session_id,
                turn_id=turn_id,
                tool_use_id="tool-use-1",
                tool_call_id="tool-call-1",
            ),
        )
    )
    snapshot = store.load_session_snapshot(session_id)
    runtime_state = store.load_runtime_state(session_id)
    skill_state = runtime_state.agent_states["finding"].invoked_skills["code-audit-finding"]

    assert payload.output_payload == {"skill": "code-audit-finding", "content": "body"}
    assert len(snapshot.skill_invocations) == 1
    assert snapshot.skill_invocations[0].skill_ref == "code-audit-finding"
    assert snapshot.skill_invocations[0].status == AuditSkillInvocationStatus.COMPLETED.value
    assert skill_state.skill_stage == "body"
    assert skill_state.invocation_count == 1
    assert skill_state.last_turn_id == turn_id