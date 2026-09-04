from __future__ import annotations

from typing import Any

from app.services.contracts.models import RuntimeSkillCatalogSnapshot
from app.services.skill.facade import SkillService
from app.services.skill.router import RUNTIME_SKILL_ROUTE_AGENT_TYPES, build_review_skill_route_message


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
        route_message = (
            build_review_skill_route_message(context, resolved)
            if agent_type in RUNTIME_SKILL_ROUTE_AGENT_TYPES
            else prompt
        )
        return RuntimeSkillCatalogSnapshot(
            available_skills=list(resolved.get("metadata") or []),
            matched_skills=list(resolved.get("matched") or []),
            prompt=prompt,
            route_message=route_message,
            route_plan=dict(resolved.get("route_plan") or {}),
        )