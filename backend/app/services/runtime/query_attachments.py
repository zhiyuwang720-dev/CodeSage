from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.contracts.models import RuntimeMessageRole, TranscriptItem
from app.services.contracts.query_state import QueryLoopState


def build_between_turn_attachments(*, state: QueryLoopState, records: list[Any], session_snapshot: Any) -> list[TranscriptItem]:
    if _native_tool_history_enabled(state):
        return []
    del session_snapshot
    if not records:
        return []
    parts = [f"{record.request.name}({record.status})" for record in records]
    return [
        TranscriptItem(
            role=RuntimeMessageRole.USER,
            content="Recent tool results: " + ", ".join(parts),
            name="tool_results_attachment",
            metadata={"synthetic": True, "kind": "tool_results_attachment", "tool_count": len(records)},
        )
    ]


def start_pending_tool_use_summary(*, state: QueryLoopState, records: list[Any], session_snapshot: Any) -> dict[str, Any] | None:
    if _native_tool_history_enabled(state):
        return None
    del session_snapshot
    if not records:
        return None
    lines: list[str] = []
    tool_names: list[str] = []
    for record in records:
        tool_names.append(record.request.name)
        excerpt = str(record.result.content or "").strip().replace("\n", " ")[:160]
        lines.append(f"- {record.request.name}: {record.status} -> {excerpt}" if excerpt else f"- {record.request.name}: {record.status}")
    return {
        "status": "ready",
        "tool_names": tool_names,
        "summary_message": "Tool-use summary:\n" + "\n".join(lines),
        "emitted": False,
    }


def materialize_pending_tool_use_summary(state: QueryLoopState) -> QueryLoopState:
    pending = dict(state.pending_tool_use_summary or {})
    if pending.get("status") != "ready":
        return state
    summary_message = str(pending.get("summary_message") or "").strip()
    if not summary_message:
        state.pending_tool_use_summary = None
        return state
    if pending.get("emitted"):
        state.pending_tool_use_summary = None
        return state
    state.messages = [
        *list(state.messages),
        TranscriptItem(
            role=RuntimeMessageRole.USER,
            content=summary_message,
            name="tool_use_summary",
            metadata={
                "synthetic": True,
                "kind": "tool_use_summary",
                "tool_names": list(pending.get("tool_names") or []),
            },
        ),
    ]
    state.pending_tool_use_summary = None
    return state


def _native_tool_history_enabled(state: QueryLoopState) -> bool:
    context = dict(state.tool_use_context or {})
    return bool(context.get("native_tool_history", True))
