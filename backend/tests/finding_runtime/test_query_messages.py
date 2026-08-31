from __future__ import annotations

from app.services.review_runtime.models import RuntimeMessageRole, TranscriptItem
from app.services.review_runtime.query_messages import normalize_messages_for_model


def test_normalize_messages_for_model_drops_empty_synthetic_and_merges_plain_user_messages():
    normalized = normalize_messages_for_model(
        [
            TranscriptItem(role=RuntimeMessageRole.SYSTEM, content="system"),
            TranscriptItem(role=RuntimeMessageRole.USER, content="inspect auth"),
            TranscriptItem(role=RuntimeMessageRole.USER, content="check IDOR"),
            TranscriptItem(
                role=RuntimeMessageRole.USER,
                content="",
                name="empty_synthetic",
                metadata={"synthetic": True},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.USER,
                content="keep me separate",
                name="tool_use_summary",
                metadata={"synthetic": True},
            ),
        ]
    )

    assert [item.role for item in normalized] == [RuntimeMessageRole.USER, RuntimeMessageRole.USER]
    assert normalized[0].content == "inspect auth\n\ncheck IDOR"
    assert normalized[0].name is None
    assert normalized[1].name == "tool_use_summary"


def test_normalize_messages_for_model_drops_hidden_hook_artifacts_from_model_input():
    normalized = normalize_messages_for_model(
        [
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content="Running TaskCompleted hook for alice",
                name="hook_progress",
                metadata={"synthetic": True, "kind": "hook_progress", "hidden_from_model": True},
            ),
            TranscriptItem(
                role=RuntimeMessageRole.SYSTEM,
                content="Stop hook summary",
                name="stop_hook_summary",
                metadata={"synthetic": True, "kind": "stop_hook_summary", "hidden_from_model": True},
            ),
            TranscriptItem(role=RuntimeMessageRole.USER, content="real user message"),
        ]
    )

    assert len(normalized) == 1
    assert normalized[0].content == "real user message"
