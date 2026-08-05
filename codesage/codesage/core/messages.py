"""Session-level messages: the internal AI contract (phase 02) plus session metadata.

A SessionMessage is what the conversation log stores and the engine loops
over: ai.Message content plus a stable uuid, cost/usage, and error flags.
ProgressMessage (transient UI placeholders) is deferred to the CLI phase (07).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from ..ai import ContentBlock, Message, Usage


@dataclass(slots=True)
class SessionMessage:
    """One durable conversation message."""

    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]
    uuid: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # assistant-only metadata
    usage: Usage | None = None
    model: str | None = None
    message_id: str | None = None  # assistant streaming chunk anchor (Kode message.id)
    is_error: bool = False  # provider error surfaced as a message; dropped before API
    is_meta: bool = False  # synthesized messages (e.g. interruption notices)
    error_message: str | None = None  # provider/transport error detail (diagnostics)

    # ---- serialization ----

    def to_dict(self) -> dict[str, Any]:
        content: Any = self.content
        if isinstance(content, list):
            content = [b.model_dump() for b in content]
        return {
            "role": self.role,
            "content": content,
            "uuid": self.uuid,
            "timestamp": self.timestamp,
            "usage": None if self.usage is None else self.usage.model_dump(),
            "model": self.model,
            "message_id": self.message_id,
            "is_error": self.is_error,
            "is_meta": self.is_meta,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMessage | None":
        if data.get("role") not in ("user", "assistant"):
            return None  # unknown role: skip (load() tolerates it)
        content = data["content"]
        if isinstance(content, list):
            content = [ContentBlock(**b) for b in content]
        usage = data.get("usage")
        return cls(
            role=data["role"],
            content=content,
            uuid=data.get("uuid") or uuid.uuid4().hex,
            timestamp=data.get("timestamp", ""),
            usage=Usage(**usage) if usage else None,
            model=data.get("model"),
            message_id=data.get("message_id"),
            is_error=bool(data.get("is_error", False)),
            is_meta=bool(data.get("is_meta", False)),
            error_message=data.get("error_message"),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def to_ai_message(self) -> Message:
        """Downgrade to the plain AI contract (for LLMRequest)."""
        return Message(role=self.role, content=self.content)


def user_message(content: str | list[ContentBlock], **kw: Any) -> SessionMessage:
    return SessionMessage(role="user", content=content, **kw)


def assistant_message(
    content: str | list[ContentBlock],
    *,
    usage: Usage | None = None,
    model: str | None = None,
    is_error: bool = False,
    **kw: Any,
) -> SessionMessage:
    return SessionMessage(
        role="assistant",
        content=content,
        usage=usage,
        model=model,
        is_error=is_error,
        **kw,
    )
