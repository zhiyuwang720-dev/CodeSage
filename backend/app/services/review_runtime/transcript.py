from __future__ import annotations

from app.models.audit_session import AuditSessionMessage
from app.services.contracts.models import RuntimeMessageRole, TranscriptItem


def to_transcript_item(message: AuditSessionMessage) -> TranscriptItem:
    return TranscriptItem(
        role=RuntimeMessageRole(message.role),
        content=message.content,
        name=message.name,
        metadata=dict(message.message_metadata or {}),
        payload=dict(message.payload or {}),
    )


def to_transcript_items(messages: list[AuditSessionMessage]) -> list[TranscriptItem]:
    return [to_transcript_item(message) for message in messages]
