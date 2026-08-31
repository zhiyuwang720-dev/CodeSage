from __future__ import annotations

from typing import Any

from app.models.audit_session import AuditSkillInvocationStatus
from app.services.agent.skill_service import SkillService


class SkillInvocationRuntime:
    def __init__(
        self,
        *,
        session_store,
        agent_type: str,
        user_id: str | None = None,
        skill_service: Any = SkillService,
    ):
        self._session_store = session_store
        self._agent_type = agent_type
        self._user_id = user_id
        self._skill_service = skill_service

    async def invoke(
        self,
        *,
        session_id: str,
        turn_id: str,
        skill_ref: str,
        action: str,
        resource_name: str | None = None,
        input_payload: dict[str, Any] | None = None,
        invocation_source: str = "model",
    ) -> dict[str, Any]:
        invocation_payload = dict(input_payload or {})
        invocation_payload.setdefault("invocation_source", invocation_source)
        if resource_name is not None and "resource_name" not in invocation_payload:
            invocation_payload["resource_name"] = resource_name
        invocation_id = self._session_store.start_skill_invocation(
            session_id=session_id,
            turn_id=turn_id,
            skill_ref=skill_ref,
            input_payload=invocation_payload,
        )
        try:
            skill_entry = self._resolve_skill_entry(skill_ref)
            self._validate_skill_invocation(skill_entry, invocation_source=invocation_source)
            data = await self._execute_action(
                skill_ref=skill_ref,
                action=action,
                resource_name=resource_name,
            )
            runtime_state = self._session_store.load_runtime_state(session_id)
            runtime_state.mark_skill_invoked(
                agent_type=self._agent_type,
                skill_ref=skill_ref,
                skill_stage=self._resolve_stage(action=action, resource_name=resource_name),
                invocation_id=invocation_id,
                turn_id=turn_id,
                loaded_resources=self._resolve_loaded_resources(action=action, resource_name=resource_name),
            )
            contract = self._build_skill_contract(skill_entry, invocation_source=invocation_source)
            if contract:
                runtime_state.record_skill_contract(
                    agent_type=self._agent_type,
                    skill_ref=skill_ref,
                    contract=contract,
                )
            self._session_store.replace_runtime_state(session_id, runtime_state)
            self._session_store.complete_skill_invocation(
                invocation_id,
                status=AuditSkillInvocationStatus.COMPLETED.value,
                output_payload=data,
            )
            return data
        except Exception as exc:
            self._session_store.complete_skill_invocation(
                invocation_id,
                status=AuditSkillInvocationStatus.FAILED.value,
                output_payload={},
                error_message=str(exc),
            )
            raise

    def _resolve_skill_entry(self, skill_ref: str):
        resolver = getattr(self._skill_service, "get_skill_entry", None)
        if resolver is None:
            return None
        return resolver(self._user_id, skill_ref, agent_type=self._agent_type)

    def _validate_skill_invocation(self, skill_entry: Any, *, invocation_source: str) -> None:
        if skill_entry is None:
            return
        if not getattr(skill_entry, "is_active", True):
            raise ValueError(f"Skill '{skill_entry.slug}' is disabled.")
        if not self._agent_matches(getattr(skill_entry, "agent", None)):
            raise PermissionError(f"Skill '{skill_entry.slug}' is not enabled for agent '{self._agent_type}'.")
        if invocation_source == "model" and bool(getattr(skill_entry, "disable_model_invocation", False)):
            raise PermissionError(f"Skill '{skill_entry.slug}' cannot be invoked directly by the model.")
        if invocation_source == "user" and not bool(getattr(skill_entry, "user_invocable", True)):
            raise PermissionError(f"Skill '{skill_entry.slug}' cannot be invoked directly by the user.")

    def _agent_matches(self, configured_agent: str | None) -> bool:
        normalized = str(configured_agent or "").strip()
        if not normalized:
            return True
        allowed = {
            part.strip().lower()
            for raw in normalized.replace("|", ",").replace("/", ",").split(",")
            for part in [raw]
            if part.strip()
        }
        if not allowed:
            return True
        return self._agent_type.strip().lower() in allowed

    @staticmethod
    def _normalize_string_list(values: list[str] | None) -> list[str]:
        normalized: list[str] = []
        for value in values or []:
            item = str(value or "").strip()
            if item and item not in normalized:
                normalized.append(item)
        return normalized

    def _build_skill_contract(self, skill_entry: Any, *, invocation_source: str) -> dict[str, Any]:
        if skill_entry is None:
            return {}
        return {
            "skill_ref": str(getattr(skill_entry, "slug", "") or getattr(skill_entry, "name", "")).strip(),
            "allowed_tools": self._normalize_string_list(getattr(skill_entry, "allowed_tools", [])),
            "model": getattr(skill_entry, "model", None),
            "effort": getattr(skill_entry, "effort", None),
            "context": getattr(skill_entry, "execution_context", None),
            "agent": getattr(skill_entry, "agent", None),
            "paths": self._normalize_string_list(getattr(skill_entry, "paths", [])),
            "hooks": dict(getattr(skill_entry, "hooks", {}) or {}),
            "disable_model_invocation": bool(getattr(skill_entry, "disable_model_invocation", False)),
            "user_invocable": bool(getattr(skill_entry, "user_invocable", True)),
            "source_type": getattr(skill_entry, "source_type", None),
            "source_url": getattr(skill_entry, "source_url", None),
            "invocation_source": invocation_source,
        }

    async def _execute_action(self, *, skill_ref: str, action: str, resource_name: str | None) -> dict[str, Any]:
        if action == "body":
            return await self._skill_service.get_skill_body(
                self._user_id,
                skill_ref,
                agent_type=self._agent_type,
            )
        if action == "list_resources":
            return await self._skill_service.list_skill_resources(
                self._user_id,
                skill_ref,
                resource_name or "",
                agent_type=self._agent_type,
            )
        if action == "read_resource":
            if not resource_name:
                raise ValueError("resource_name is required for read_resource")
            return await self._skill_service.get_skill_resource(
                self._user_id,
                skill_ref,
                resource_name,
                agent_type=self._agent_type,
            )
        raise ValueError(f"Unsupported skill action: {action}")

    @staticmethod
    def _normalize_resource_name(resource_name: str | None) -> str:
        return str(resource_name or "").replace("\\", "/").strip(" /")

    @classmethod
    def _resolve_loaded_resources(cls, *, action: str, resource_name: str | None) -> list[str]:
        if action != "read_resource":
            return []
        normalized = cls._normalize_resource_name(resource_name)
        return [normalized] if normalized else []

    @classmethod
    def _resolve_stage(cls, *, action: str, resource_name: str | None) -> str:
        if action == "body":
            return "body"
        normalized = cls._normalize_resource_name(resource_name)
        if normalized.startswith("scripts/") or normalized == "scripts":
            return "scripts"
        if normalized.startswith("examples/") or normalized == "examples":
            return "examples"
        if normalized:
            return "references"
        if action == "list_resources":
            return "references"
        return "catalog"