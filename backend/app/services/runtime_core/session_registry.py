from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any

from .session_state import SessionRuntimeState


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeSessionRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._agent_index: dict[str, str] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._agent_index.clear()

    def upsert(
        self,
        *,
        session_key: str,
        runtime_state: SessionRuntimeState,
        agent_id: str,
        agent_type: str,
        task_id: str | None = None,
        source: str = "runtime",
    ) -> dict[str, Any]:
        normalized_key = str(session_key or "").strip()
        normalized_agent = str(agent_id or "").strip()
        normalized_type = str(agent_type or "").strip()
        normalized_task = str(task_id or "").strip() or None
        if not normalized_key:
            raise ValueError("session_key is required")
        if not normalized_agent:
            raise ValueError("agent_id is required")
        with self._lock:
            entry = {
                "session_key": normalized_key,
                "session_id": runtime_state.session_id,
                "task_id": normalized_task,
                "agent_id": normalized_agent,
                "agent_type": normalized_type,
                "source": str(source or "runtime").strip() or "runtime",
                "updated_at": _utc_now(),
                "runtime_state": runtime_state.model_dump(),
            }
            self._entries[normalized_key] = entry
            self._agent_index[normalized_agent] = normalized_key
            return dict(entry)

    def get(self, session_key: str) -> dict[str, Any] | None:
        normalized_key = str(session_key or "").strip()
        if not normalized_key:
            return None
        with self._lock:
            entry = self._entries.get(normalized_key)
            return dict(entry) if entry else None

    def get_by_agent(self, agent_id: str) -> dict[str, Any] | None:
        normalized_agent = str(agent_id or "").strip()
        if not normalized_agent:
            return None
        with self._lock:
            session_key = self._agent_index.get(normalized_agent)
            if not session_key:
                return None
            entry = self._entries.get(session_key)
            return dict(entry) if entry else None

    def list_by_task(self, task_id: str) -> list[dict[str, Any]]:
        normalized_task = str(task_id or "").strip()
        if not normalized_task:
            return []
        with self._lock:
            items = [dict(entry) for entry in self._entries.values() if entry.get("task_id") == normalized_task]
        return sorted(items, key=lambda item: (item.get("updated_at") or "", item.get("session_key") or ""), reverse=True)


runtime_session_registry = RuntimeSessionRegistry()
