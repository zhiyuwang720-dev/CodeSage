from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.review_runtime.models import RuntimeContinueReason, RuntimeMessageRole, TranscriptItem


@dataclass(slots=True)
class QueryLoopState:
    messages: list[TranscriptItem] = field(default_factory=list)
    tool_use_context: dict[str, Any] = field(default_factory=dict)
    auto_compact_tracking: dict[str, Any] | None = None
    context_collapse_state: dict[str, Any] | None = None
    max_output_tokens_recovery_count: int = 0
    has_attempted_reactive_compact: bool = False
    max_output_tokens_override: int | None = None
    pending_tool_use_summary: dict[str, Any] | None = None
    stop_hook_active: bool | None = None
    turn_count: int = 1
    transition: RuntimeContinueReason | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "role": item.role.value,
                    "content": item.content,
                    "name": item.name,
                    "metadata": dict(item.metadata),
                    "payload": dict(item.payload),
                }
                for item in self.messages
            ],
            "tool_use_context": dict(self.tool_use_context),
            "auto_compact_tracking": dict(self.auto_compact_tracking) if self.auto_compact_tracking is not None else None,
            "context_collapse_state": dict(self.context_collapse_state) if self.context_collapse_state is not None else None,
            "max_output_tokens_recovery_count": int(self.max_output_tokens_recovery_count),
            "has_attempted_reactive_compact": bool(self.has_attempted_reactive_compact),
            "max_output_tokens_override": self.max_output_tokens_override,
            "pending_tool_use_summary": dict(self.pending_tool_use_summary) if self.pending_tool_use_summary is not None else None,
            "stop_hook_active": self.stop_hook_active,
            "turn_count": int(self.turn_count),
            "transition": self.transition.value if self.transition is not None else None,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> QueryLoopState:
        raw = dict(payload or {})
        messages: list[TranscriptItem] = []
        for item in raw.get("messages") or []:
            if not isinstance(item, dict):
                continue
            role_value = item.get("role") or RuntimeMessageRole.USER.value
            try:
                role = RuntimeMessageRole(role_value)
            except ValueError:
                role = RuntimeMessageRole.USER
            messages.append(
                TranscriptItem(
                    role=role,
                    content=str(item.get("content") or ""),
                    name=str(item.get("name")) if item.get("name") is not None else None,
                    metadata=dict(item.get("metadata") or {}),
                    payload=dict(item.get("payload") or {}),
                )
            )
        transition_value = raw.get("transition")
        transition = None
        if transition_value:
            try:
                transition = RuntimeContinueReason(str(transition_value))
            except ValueError:
                transition = None
        return cls(
            messages=messages,
            tool_use_context=dict(raw.get("tool_use_context") or {}),
            auto_compact_tracking=dict(raw.get("auto_compact_tracking") or {}) if raw.get("auto_compact_tracking") is not None else None,
            context_collapse_state=dict(raw.get("context_collapse_state") or {}) if raw.get("context_collapse_state") is not None else None,
            max_output_tokens_recovery_count=int(raw.get("max_output_tokens_recovery_count") or 0),
            has_attempted_reactive_compact=bool(raw.get("has_attempted_reactive_compact") or False),
            max_output_tokens_override=raw.get("max_output_tokens_override"),
            pending_tool_use_summary=dict(raw.get("pending_tool_use_summary") or {}) if raw.get("pending_tool_use_summary") is not None else None,
            stop_hook_active=raw.get("stop_hook_active"),
            turn_count=max(1, int(raw.get("turn_count") or 1)),
            transition=transition,
        )
