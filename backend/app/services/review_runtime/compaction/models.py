from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.review_runtime.models import TranscriptItem


@dataclass(slots=True)
class AutoCompactTrackingState:
    compacted: bool
    turn_counter: int
    turn_id: str
    consecutive_failures: int | None = None


@dataclass(slots=True)
class CompactionResult:
    boundary_marker: TranscriptItem
    summary_messages: list[TranscriptItem] = field(default_factory=list)
    attachments: list[TranscriptItem] = field(default_factory=list)
    hook_results: list[TranscriptItem] = field(default_factory=list)
    messages_to_keep: list[TranscriptItem] | None = None
    user_display_message: str | None = None
    pre_compact_token_count: int | None = None
    post_compact_token_count: int | None = None
    true_post_compact_token_count: int | None = None
    compaction_usage: dict[str, Any] | None = None
