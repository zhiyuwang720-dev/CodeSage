from __future__ import annotations

import json
from hashlib import sha1
from typing import Any

from sqlalchemy import select

from app.models.agent_task import AgentCheckpoint
from app.services.runtime_core.session_registry import runtime_session_registry
from app.services.runtime_core.session_state import SessionRuntimeState, sync_legacy_agent_metadata_from_runtime_state


class RuntimeSessionCheckpointStore:
    def __init__(self, *, session_factory):
        self._session_factory = session_factory

    async def persist_agent_runtime_session_checkpoint(
        self,
        *,
        task_id: str,
        agent_state: Any,
        checkpoint_type: str = "auto",
        checkpoint_name: str = "runtime_session_state",
    ) -> AgentCheckpoint | None:
        runtime_session_state = dict((getattr(agent_state, "metadata", {}) or {}).get("runtime_session_state") or {})
        runtime_session_ref = dict((getattr(agent_state, "metadata", {}) or {}).get("runtime_session_ref") or {})
        if not runtime_session_state:
            return None

        payload = {
            "runtime_session_ref": runtime_session_ref,
            "runtime_session_state": runtime_session_state,
        }
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        payload_hash = sha1(payload_json.encode("utf-8")).hexdigest()
        metadata = getattr(agent_state, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            setattr(agent_state, "metadata", metadata)
        if metadata.get("_last_runtime_session_checkpoint_hash") == payload_hash:
            return None

        checkpoint = AgentCheckpoint(
            task_id=str(task_id),
            agent_id=str(getattr(agent_state, "agent_id", "")),
            agent_name=str(getattr(agent_state, "agent_name", "")),
            agent_type=str(getattr(agent_state, "agent_type", "")),
            parent_agent_id=getattr(agent_state, "parent_id", None),
            state_data=json.dumps(agent_state.model_dump(), ensure_ascii=False),
            iteration=int(getattr(agent_state, "iteration", 0) or 0),
            status=str(getattr(agent_state, "status", "created")),
            total_tokens=int(getattr(agent_state, "total_tokens", 0) or 0),
            tool_calls=int(getattr(agent_state, "tool_calls", 0) or 0),
            findings_count=len(getattr(agent_state, "findings", []) or []),
            checkpoint_type=str(checkpoint_type or "auto"),
            checkpoint_name=str(checkpoint_name or "runtime_session_state"),
            checkpoint_metadata=payload,
        )

        async with self._session_factory() as db:
            db.add(checkpoint)
            await db.commit()
            await db.refresh(checkpoint)

        metadata["_last_runtime_session_checkpoint_hash"] = payload_hash
        metadata["last_runtime_session_checkpoint_id"] = checkpoint.id
        return checkpoint


    async def restore_agent_runtime_session_checkpoint(
        self,
        *,
        task_id: str,
        agent_state: Any,
        checkpoint_name: str = "runtime_session_state",
    ) -> dict[str, Any] | None:
        async with self._session_factory() as db:
            result = await db.execute(
                select(AgentCheckpoint)
                .where(AgentCheckpoint.task_id == str(task_id))
                .where(AgentCheckpoint.agent_id == str(getattr(agent_state, "agent_id", "")))
                .where(AgentCheckpoint.checkpoint_name == str(checkpoint_name or "runtime_session_state"))
                .order_by(AgentCheckpoint.created_at.desc())
            )
            checkpoint = result.scalars().first()

        if checkpoint is None:
            return None
        payload = dict(checkpoint.checkpoint_metadata or {})
        runtime_payload = payload.get("runtime_session_state") or {}
        if not runtime_payload:
            return None

        runtime_state = SessionRuntimeState.model_validate(runtime_payload)
        metadata = getattr(agent_state, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            setattr(agent_state, "metadata", metadata)
        interaction_state = metadata.setdefault("interaction_runtime", {})
        tool_runtime = metadata.setdefault("tool_runtime", {})
        sync_legacy_agent_metadata_from_runtime_state(
            runtime_state,
            agent_type=str(getattr(agent_state, "agent_type", "")),
            interaction_state=interaction_state,
            tool_runtime=tool_runtime,
            memory_runtime=metadata.setdefault("memory_runtime", {}),
        )
        metadata["runtime_session_state"] = runtime_state.model_dump()

        runtime_session_ref = dict(payload.get("runtime_session_ref") or {})
        session_key = str(runtime_session_ref.get("session_key") or f"legacy:{task_id}:{getattr(agent_state, 'agent_id', '')}")
        entry = runtime_session_registry.upsert(
            session_key=session_key,
            runtime_state=runtime_state,
            agent_id=str(getattr(agent_state, "agent_id", "")),
            agent_type=str(getattr(agent_state, "agent_type", "")),
            task_id=str(task_id),
            source=str(runtime_session_ref.get("source") or "legacy"),
        )
        metadata["runtime_session_ref"] = {
            "session_key": entry["session_key"],
            "session_id": entry["session_id"],
            "task_id": entry["task_id"],
            "agent_id": entry["agent_id"],
            "agent_type": entry["agent_type"],
            "source": entry["source"],
            "updated_at": entry["updated_at"],
        }

        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        metadata["_last_runtime_session_checkpoint_hash"] = sha1(payload_json.encode("utf-8")).hexdigest()
        metadata["last_runtime_session_checkpoint_id"] = checkpoint.id
        return {
            "checkpoint_id": checkpoint.id,
            "runtime_session_ref": dict(metadata["runtime_session_ref"]),
        }
