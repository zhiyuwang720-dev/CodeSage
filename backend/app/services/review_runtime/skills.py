from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.services.agent.agents.finding_skill_router import build_finding_skill_route_message
from app.services.agent.skill_service import SkillService
from app.services.contracts.models import RuntimeSkillCatalogSnapshot, ToolExecutionPayload
from app.services.review_runtime.tooling import RuntimeTool, ToolExecutionContext
from app.services.runtime_core.skill_runtime import SkillInvocationRuntime


class RuntimeSkillCatalog:
    def __init__(self, *, skill_service: Any = SkillService):
        self._skill_service = skill_service

    async def preload(
        self,
        *,
        user_id: str | None,
        agent_type: str,
        context: dict[str, Any],
    ) -> RuntimeSkillCatalogSnapshot:
        resolved = await self._skill_service.resolve_agent_skills(user_id, agent_type, context)
        prompt = self._skill_service.build_skill_briefing(resolved)
        route_message = build_finding_skill_route_message(context, resolved) if agent_type == "finding" else prompt
        return RuntimeSkillCatalogSnapshot(
            available_skills=list(resolved.get("metadata") or []),
            matched_skills=list(resolved.get("matched") or []),
            prompt=prompt,
            route_message=route_message,
            route_plan=dict(resolved.get("route_plan") or {}),
        )


class InvokeSkillInput(BaseModel):
    skill_ref: str = Field(default="code-audit-finding", description="Skill slug, id, or display name.")
    action: str = Field(default="body", description="One of: body, list_resources, read_resource.")
    resource_name: str | None = Field(default=None, description="Relative resource path for resource reads.")


class RuntimeSkillTool(RuntimeTool):
    name = "Skill"
    description = (
        "Load instructions and resources from the local skill library. "
        "When a skill matches the user's task or is explicitly named in the prompt, invoking this tool is a blocking requirement: "
        "you must call Skill(action=\"body\") for the relevant skill before producing any other task-specific answer, audit, or plan. "
        "After reading the SKILL.md body, follow its startup protocol and mandatory reads by calling "
        "Skill(action=\"read_resource\", resource_name=...) for each referenced checklist, coverage matrix, guide, example, or script that it requires. "
        "Use list_resources only for discovery; do not treat discovery metadata as a substitute for reading the required skill body and resources."
    )
    input_model = InvokeSkillInput

    def __init__(
        self,
        *,
        session_store,
        agent_type: str = "finding",
        user_id: str | None = None,
        skill_service: Any = SkillService,
    ):
        self._session_store = session_store
        self._agent_type = agent_type
        self._user_id = user_id
        self._skill_service = skill_service
        self._runtime = SkillInvocationRuntime(
            session_store=session_store,
            agent_type=agent_type,
            user_id=user_id,
            skill_service=skill_service,
        )

    def validate_input(self, raw_input: dict[str, Any]) -> InvokeSkillInput:
        payload = dict(raw_input or {})
        normalized = {
            "skill_ref": payload.get("skill_ref") or payload.get("skill") or payload.get("name") or "code-audit-finding",
            "action": payload.get("action") or payload.get("mode") or payload.get("operation") or "body",
            "resource_name": payload.get("resource_name") or payload.get("resource") or payload.get("path"),
        }
        return InvokeSkillInput.model_validate(normalized)

    def is_concurrency_safe(self, parsed_input: Any) -> bool:
        return True

    async def execute(self, parsed_input: InvokeSkillInput, context: ToolExecutionContext) -> ToolExecutionPayload:
        data = await self._runtime.invoke(
            session_id=context.session_id,
            turn_id=context.turn_id,
            skill_ref=parsed_input.skill_ref,
            action=parsed_input.action,
            resource_name=parsed_input.resource_name,
            input_payload=parsed_input.model_dump(),
        )
        return ToolExecutionPayload(
            content=f"Skill {parsed_input.skill_ref} {parsed_input.action} completed",
            output_payload=data,
            metadata={"skill_ref": parsed_input.skill_ref, "action": parsed_input.action},
        )
