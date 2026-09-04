from __future__ import annotations

from app.services.contracts.models import RuntimeMessageRole, TranscriptItem


def normalize_messages_for_model(messages: list[TranscriptItem]) -> list[TranscriptItem]:
    normalized: list[TranscriptItem] = []
    for item in messages:
        if item.role is RuntimeMessageRole.SYSTEM:
            continue
        if _should_drop_message(item):
            continue
        cloned = TranscriptItem(
            role=item.role,
            content=item.content,
            name=item.name,
            metadata=dict(item.metadata),
            payload=dict(item.payload),
        )
        if _can_merge_plain_user_message(normalized, cloned):
            normalized[-1].content = f"{normalized[-1].content}\n\n{cloned.content}"
            continue
        normalized.append(cloned)
    return normalized


def _should_drop_message(item: TranscriptItem) -> bool:
    if str(item.content or "").strip():
        return False
    return bool(item.metadata.get("synthetic")) and not item.payload


def _can_merge_plain_user_message(normalized: list[TranscriptItem], item: TranscriptItem) -> bool:
    if item.role is not RuntimeMessageRole.USER:
        return False
    if item.name is not None or item.metadata or item.payload:
        return False
    if not normalized:
        return False
    previous = normalized[-1]
    return previous.role is RuntimeMessageRole.USER and previous.name is None and not previous.metadata and not previous.payload
