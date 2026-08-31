import pytest

from app.services.agent.skill_service import SkillService
from app.services.agent.tools.skill_tool import SkillBodyTool, SkillResourceTool


@pytest.mark.asyncio
async def test_skill_body_tool_forwards_agent_type(monkeypatch):
    called = {}

    async def fake_get_skill_body(user_id, skill_ref, agent_type=None):
        called["user_id"] = user_id
        called["skill_ref"] = skill_ref
        called["agent_type"] = agent_type
        return {"slug": skill_ref}

    monkeypatch.setattr(SkillService, "get_skill_body", fake_get_skill_body)

    result = await SkillBodyTool(user_id="u1", agent_type="finding").execute(skill_ref="alpha")

    assert result.success is True
    assert called["agent_type"] == "finding"


@pytest.mark.asyncio
async def test_skill_resource_tool_forwards_agent_type(monkeypatch):
    called = {}

    async def fake_get_skill_resource(user_id, skill_ref, resource_name, agent_type=None):
        called["user_id"] = user_id
        called["skill_ref"] = skill_ref
        called["resource_name"] = resource_name
        called["agent_type"] = agent_type
        return {"resource": resource_name}

    monkeypatch.setattr(SkillService, "get_skill_resource", fake_get_skill_resource)

    result = await SkillResourceTool(user_id="u1", agent_type="finding").execute(
        mode="read",
        skill_ref="alpha",
        resource_name="references/core/guide.md",
    )

    assert result.success is True
    assert called["agent_type"] == "finding"
