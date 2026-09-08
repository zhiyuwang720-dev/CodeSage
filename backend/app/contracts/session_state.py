from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

_STAGE_ORDER = {
    "catalog": 0,
    "body": 1,
    "references": 2,
    "examples": 3,
    "scripts": 4,
    "full": 5,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_string_list(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in normalized:
            normalized.append(item)
    return normalized


class InvokedSkillState(BaseModel):
    skill_ref: str
    skill_stage: str = "catalog"
    invocation_count: int = 0
    first_invoked_at: str = Field(default_factory=_utc_now)
    last_invoked_at: str = Field(default_factory=_utc_now)
    last_invocation_id: str | None = None
    last_turn_id: str | None = None
    loaded_resources: list[str] = Field(default_factory=list)

    def promote_stage(self, next_stage: str) -> None:
        current_rank = _STAGE_ORDER.get(self.skill_stage, 0)
        next_rank = _STAGE_ORDER.get(str(next_stage or "catalog"), current_rank)
        if next_rank >= current_rank:
            self.skill_stage = str(next_stage or self.skill_stage)

    def mark_invoked(
        self,
        *,
        skill_stage: str,
        invocation_id: str | None = None,
        turn_id: str | None = None,
        loaded_resources: list[str] | None = None,
    ) -> None:
        if self.invocation_count <= 0:
            self.first_invoked_at = _utc_now()
        self.invocation_count += 1
        self.promote_stage(skill_stage)
        self.last_invoked_at = _utc_now()
        self.last_invocation_id = invocation_id
        self.last_turn_id = turn_id
        for resource in loaded_resources or []:
            normalized = str(resource).strip()
            if normalized and normalized not in self.loaded_resources:
                self.loaded_resources.append(normalized)


class AgentRuntimeState(BaseModel):
    agent_type: str
    invoked_skills: dict[str, InvokedSkillState] = Field(default_factory=dict)
    pending_todos: list[dict[str, Any]] = Field(default_factory=list)
    pending_questions: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def mark_skill_invoked(
        self,
        *,
        skill_ref: str,
        skill_stage: str,
        invocation_id: str | None = None,
        turn_id: str | None = None,
        loaded_resources: list[str] | None = None,
    ) -> InvokedSkillState:
        normalized_ref = str(skill_ref).strip()
        if not normalized_ref:
            raise ValueError("skill_ref is required")
        state = self.invoked_skills.get(normalized_ref)
        if state is None:
            state = InvokedSkillState(skill_ref=normalized_ref)
            self.invoked_skills[normalized_ref] = state
        state.mark_invoked(
            skill_stage=skill_stage,
            invocation_id=invocation_id,
            turn_id=turn_id,
            loaded_resources=loaded_resources,
        )
        return state.model_copy(deep=True)

    def record_skill_contract(self, *, skill_ref: str, contract: dict[str, Any]) -> dict[str, Any]:
        runtime_metadata = self.metadata.setdefault("skill_runtime", {})
        active_skills = runtime_metadata.setdefault("active_skills", {})
        normalized_ref = str(skill_ref).strip()
        active_skills[normalized_ref] = dict(contract or {})
        runtime_metadata["last_skill_ref"] = normalized_ref
        runtime_metadata["updated_at"] = _utc_now()
        return dict(active_skills[normalized_ref])


class SessionRuntimeState(BaseModel):
    session_id: str
    permission_mode: str = "default"
    touched_paths: list[str] = Field(default_factory=list)
    pending_questions: list[dict[str, Any]] = Field(default_factory=list)
    agent_states: dict[str, AgentRuntimeState] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def ensure_agent_state(self, agent_type: str) -> AgentRuntimeState:
        normalized_agent = str(agent_type).strip()
        if not normalized_agent:
            raise ValueError("agent_type is required")
        state = self.agent_states.get(normalized_agent)
        if state is None:
            state = AgentRuntimeState(agent_type=normalized_agent)
            self.agent_states[normalized_agent] = state
        return state

    def mark_skill_invoked(
        self,
        *,
        agent_type: str,
        skill_ref: str,
        skill_stage: str,
        invocation_id: str | None = None,
        turn_id: str | None = None,
        loaded_resources: list[str] | None = None,
    ) -> InvokedSkillState:
        agent_state = self.ensure_agent_state(agent_type)
        return agent_state.mark_skill_invoked(
            skill_ref=skill_ref,
            skill_stage=skill_stage,
            invocation_id=invocation_id,
            turn_id=turn_id,
            loaded_resources=loaded_resources,
        )

    def record_skill_contract(self, *, agent_type: str, skill_ref: str, contract: dict[str, Any]) -> dict[str, Any]:
        normalized_contract = dict(contract or {})
        normalized_paths = _normalize_string_list(normalized_contract.get("paths") or [])
        normalized_contract["paths"] = normalized_paths
        agent_state = self.ensure_agent_state(agent_type)
        stored_contract = agent_state.record_skill_contract(skill_ref=skill_ref, contract=normalized_contract)
        for path in normalized_paths:
            if path not in self.touched_paths:
                self.touched_paths.append(path)
        session_hooks = self.metadata.setdefault("session_hooks", {})
        if stored_contract.get("hooks"):
            session_hooks[str(skill_ref).strip()] = stored_contract["hooks"]
        self.metadata["last_skill_contract_at"] = _utc_now()
        return stored_contract

    def record_skill_catalog_snapshot(
        self,
        *,
        agent_type: str,
        available_skills: list[str],
        matched_skills: list[str],
        primary_skill: str | None = None,
    ) -> None:
        catalog_state = self.metadata.setdefault("skill_catalog", {})
        catalog_state[str(agent_type).strip()] = {
            "available_skills": _normalize_string_list(available_skills),
            "matched_skills": _normalize_string_list(matched_skills),
            "primary_skill": str(primary_skill).strip() or None,
            "updated_at": _utc_now(),
        }

    def record_skill_discovery_snapshot(
        self,
        *,
        agent_type: str,
        selected_skill: str | None,
        ranked_candidates: list[dict[str, Any]],
        latest_user_message: str | None = None,
    ) -> None:
        discovery_state = self.metadata.setdefault("skill_discovery", {})
        discovery_state[str(agent_type).strip()] = {
            "selected_skill": str(selected_skill).strip() or None,
            "ranked_candidates": [dict(item) for item in ranked_candidates],
            "latest_user_message": str(latest_user_message or "").strip() or None,
            "updated_at": _utc_now(),
        }

    def list_invoked_skills(self, agent_type: str) -> list[str]:
        state = self.agent_states.get(str(agent_type).strip())
        if state is None:
            return []
        return sorted(state.invoked_skills.keys())


def build_legacy_agent_runtime_state(
    *,
    session_id: str,
    agent_type: str,
    interaction_state: dict[str, Any] | None = None,
    tool_runtime: dict[str, Any] | None = None,
    memory_runtime: dict[str, Any] | None = None,
) -> SessionRuntimeState:
    stored_interaction = dict(interaction_state or {})
    stored_tool_runtime = dict(tool_runtime or {})
    stored_memory_runtime = dict(memory_runtime or {})
    runtime_state = SessionRuntimeState(
        session_id=str(session_id).strip(),
        permission_mode=str(stored_interaction.get("permission_mode") or "default"),
        pending_questions=[dict(item) for item in stored_interaction.get("pending_questions") or []],
        metadata={
            "plan_mode": dict(stored_interaction.get("plan_mode") or {"active": False}),
            "todos": {str(key): dict(value) for key, value in (stored_interaction.get("todos") or {}).items()},
            "questions": {str(key): dict(value) for key, value in (stored_interaction.get("questions") or {}).items()},
            "permission_rules": {str(key): dict(value) for key, value in (stored_interaction.get("permission_rules") or {}).items()},
            "session_hooks": {str(key): dict(value) for key, value in (stored_tool_runtime.get("session_hooks") or {}).items()},
            "tool_runtime": {
                "records": [dict(item) for item in stored_tool_runtime.get("records") or []],
                "events": [dict(item) for item in stored_tool_runtime.get("events") or []],
                "hook_records": [dict(item) for item in stored_tool_runtime.get("hook_records") or []],
                "checkpoints": [dict(item) for item in stored_tool_runtime.get("checkpoints") or []],
                "session_hooks": {str(key): dict(value) for key, value in (stored_tool_runtime.get("session_hooks") or {}).items()},
            },
            "memory_runtime": {
                "base_system_prompt": str(stored_memory_runtime.get("base_system_prompt") or "") or None,
                "instructions": [dict(item) for item in stored_memory_runtime.get("instructions") or []],
                "recalls": [dict(item) for item in stored_memory_runtime.get("recalls") or []],
                "source": str(stored_memory_runtime.get("source") or "") or None,
                "loaded_at": str(stored_memory_runtime.get("loaded_at") or "") or None,
            },
        },
    )
    agent_state = runtime_state.ensure_agent_state(agent_type)
    agent_state.pending_todos = [dict(item) for item in stored_interaction.get("pending_todos") or []]
    agent_state.pending_questions = [dict(item) for item in stored_interaction.get("pending_questions") or []]
    agent_state.metadata["tool_runtime"] = {
        "records": [dict(item) for item in stored_tool_runtime.get("records") or []],
        "events": [dict(item) for item in stored_tool_runtime.get("events") or []],
        "hook_records": [dict(item) for item in stored_tool_runtime.get("hook_records") or []],
        "checkpoints": [dict(item) for item in stored_tool_runtime.get("checkpoints") or []],
        "session_hooks": {str(key): dict(value) for key, value in (stored_tool_runtime.get("session_hooks") or {}).items()},
    }
    return runtime_state


def sync_legacy_agent_metadata_from_runtime_state(
    runtime_state: SessionRuntimeState,
    *,
    agent_type: str,
    interaction_state: dict[str, Any] | None = None,
    tool_runtime: dict[str, Any] | None = None,
    memory_runtime: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stored_interaction = interaction_state if interaction_state is not None else {}
    stored_tool_runtime = tool_runtime if tool_runtime is not None else {}
    stored_memory_runtime = memory_runtime if memory_runtime is not None else {}

    agent_state = runtime_state.ensure_agent_state(agent_type)
    stored_interaction["pending_todos"] = [dict(item) for item in agent_state.pending_todos]
    stored_interaction["pending_questions"] = [dict(item) for item in agent_state.pending_questions]
    stored_interaction["plan_mode"] = dict(runtime_state.metadata.get("plan_mode") or {"active": False})
    stored_interaction["todos"] = {str(key): dict(value) for key, value in (runtime_state.metadata.get("todos") or {}).items()}
    stored_interaction["questions"] = {str(key): dict(value) for key, value in (runtime_state.metadata.get("questions") or {}).items()}
    stored_interaction["permission_mode"] = runtime_state.permission_mode
    stored_interaction["permission_rules"] = {str(key): dict(value) for key, value in (runtime_state.metadata.get("permission_rules") or {}).items()}

    tool_payload = agent_state.metadata.get("tool_runtime") or runtime_state.metadata.get("tool_runtime") or {}
    stored_tool_runtime["records"] = [dict(item) for item in tool_payload.get("records") or []]
    stored_tool_runtime["events"] = [dict(item) for item in tool_payload.get("events") or []]
    stored_tool_runtime["hook_records"] = [dict(item) for item in tool_payload.get("hook_records") or []]
    stored_tool_runtime["checkpoints"] = [dict(item) for item in tool_payload.get("checkpoints") or []]
    stored_tool_runtime["session_hooks"] = {str(key): dict(value) for key, value in (runtime_state.metadata.get("session_hooks") or {}).items()}

    memory_payload = runtime_state.metadata.get("memory_runtime") or {}
    stored_memory_runtime.clear()
    stored_memory_runtime.update({
        "base_system_prompt": memory_payload.get("base_system_prompt"),
        "instructions": [dict(item) for item in memory_payload.get("instructions") or []],
        "recalls": [dict(item) for item in memory_payload.get("recalls") or []],
        "source": memory_payload.get("source"),
        "loaded_at": memory_payload.get("loaded_at"),
    })
    return stored_interaction, stored_tool_runtime
