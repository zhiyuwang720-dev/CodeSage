from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.review_runtime.session_store import AuditSessionStore
from app.services.runtime_core.skill_runtime import SkillInvocationRuntime
from app.services.skills_runtime.models import SkillEntry


class FakeSkillService:
    @staticmethod
    def get_skill_entry(user_id, skill_ref, agent_type=None):
        return SkillEntry(
            slug=skill_ref,
            name="Code Audit Finding",
            description="Primary finding skill",
            skill_file="skill_library/code-audit-finding/SKILL.md",
            folder_path="skill_library/code-audit-finding",
            allowed_tools=["Read", "Grep"],
            model="gpt-5.4",
            effort="high",
            execution_context="workspace-write",
            agent="finding",
            hooks={"Stop": [{"matcher": "*", "hooks": ["notify"]}]},
            paths=["src/auth", "src/session.py"],
            disable_model_invocation=False,
            user_invocable=True,
            source_type="bundled",
            source_url="https://example.invalid/skill",
        )

    @staticmethod
    async def get_skill_body(user_id, skill_ref, agent_type=None):
        return {"skill": skill_ref, "content": "body"}

    @staticmethod
    async def list_skill_resources(user_id, skill_ref, resource_name="", agent_type=None):
        return {"skill": skill_ref, "mode": "list", "resource_name": resource_name, "items": []}

    @staticmethod
    async def get_skill_resource(user_id, skill_ref, resource_name, agent_type=None):
        return {"skill": skill_ref, "resource": resource_name, "content": "resource body"}


class FakeRestrictedSkillService(FakeSkillService):
    @staticmethod
    def get_skill_entry(user_id, skill_ref, agent_type=None):
        entry = FakeSkillService.get_skill_entry(user_id, skill_ref, agent_type)
        entry.disable_model_invocation = True
        entry.user_invocable = False
        return entry


def build_store() -> AuditSessionStore:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return AuditSessionStore(session_factory=session_factory)


def test_skill_invocation_runtime_tracks_progressive_state_and_resources():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    runtime = SkillInvocationRuntime(
        session_store=store,
        agent_type="finding",
        user_id=None,
        skill_service=FakeSkillService(),
    )

    body_result = asyncio.run(
        runtime.invoke(
            session_id=session_id,
            turn_id=turn_id,
            skill_ref="code-audit-finding",
            action="body",
        )
    )
    resource_result = asyncio.run(
        runtime.invoke(
            session_id=session_id,
            turn_id=turn_id,
            skill_ref="code-audit-finding",
            action="read_resource",
            resource_name="references/checklist.md",
        )
    )

    runtime_state = store.load_runtime_state(session_id)
    skill_state = runtime_state.agent_states["finding"].invoked_skills["code-audit-finding"]
    invocations = store.list_skill_invocations(session_id)

    assert body_result["content"] == "body"
    assert resource_result["resource"] == "references/checklist.md"
    assert len(invocations) == 2
    assert skill_state.skill_stage == "references"
    assert skill_state.invocation_count == 2
    assert skill_state.last_turn_id == turn_id
    assert skill_state.loaded_resources == ["references/checklist.md"]


def test_skill_invocation_runtime_records_frontmatter_contract_in_session_state():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    runtime = SkillInvocationRuntime(
        session_store=store,
        agent_type="finding",
        user_id=None,
        skill_service=FakeSkillService(),
    )

    asyncio.run(
        runtime.invoke(
            session_id=session_id,
            turn_id=turn_id,
            skill_ref="code-audit-finding",
            action="body",
        )
    )

    runtime_state = store.load_runtime_state(session_id)
    agent_metadata = runtime_state.agent_states["finding"].metadata["skill_runtime"]
    contract = agent_metadata["active_skills"]["code-audit-finding"]

    assert contract["allowed_tools"] == ["Read", "Grep"]
    assert contract["model"] == "gpt-5.4"
    assert contract["effort"] == "high"
    assert contract["context"] == "workspace-write"
    assert contract["agent"] == "finding"
    assert contract["paths"] == ["src/auth", "src/session.py"]
    assert contract["hooks"] == {"Stop": [{"matcher": "*", "hooks": ["notify"]}]}
    assert runtime_state.touched_paths == ["src/auth", "src/session.py"]
    assert runtime_state.metadata["session_hooks"]["code-audit-finding"] == contract["hooks"]


def test_skill_invocation_runtime_blocks_frontmatter_restricted_invocations():
    store = build_store()
    session_id = store.create_session(project_id="project-1")
    turn_id = store.open_turn(session_id, model_name="gpt-test")
    runtime = SkillInvocationRuntime(
        session_store=store,
        agent_type="finding",
        user_id=None,
        skill_service=FakeRestrictedSkillService(),
    )

    with pytest.raises(PermissionError):
        asyncio.run(
            runtime.invoke(
                session_id=session_id,
                turn_id=turn_id,
                skill_ref="code-audit-finding",
                action="body",
                invocation_source="model",
            )
        )

    with pytest.raises(PermissionError):
        asyncio.run(
            runtime.invoke(
                session_id=session_id,
                turn_id=turn_id,
                skill_ref="code-audit-finding",
                action="body",
                invocation_source="user",
            )
        )