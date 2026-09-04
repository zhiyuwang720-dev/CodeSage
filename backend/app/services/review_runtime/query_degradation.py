from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from app.services.review_runtime.compaction.compact import partial_compact_conversation
from app.services.review_runtime.compaction.post_compact import build_post_compact_messages
from app.services.contracts.models import (
    RuntimeContinueReason,
    RuntimeMessageRole,
    RuntimeModelResponse,
    RuntimeStopReason,
    TranscriptItem,
)
from app.services.review_runtime.query_context import recover_context_collapse_from_overflow
from app.services.contracts.query_state import QueryLoopState

ESCALATED_MAX_OUTPUT_TOKENS = 64000
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3
CONTINUATION_NUDGE = "Continue from where you left off and finish the current answer without repeating earlier points."


@dataclass(slots=True)
class DegradationDecision:
    next_state: QueryLoopState | None = None
    stop_reason: RuntimeStopReason | None = None
    checkpoint_payload: dict[str, object] | None = None


async def handle_recoverable_response(
    *,
    state: QueryLoopState,
    working_messages: list[TranscriptItem],
    model_response: RuntimeModelResponse,
    model: str | None = None,
    model_client=None,
) -> DegradationDecision | None:
    kind = str(model_response.recoverable_error_kind or "").strip()
    if not kind:
        return None

    if kind == "max_output_tokens":
        if state.max_output_tokens_override is None:
            return DegradationDecision(
                next_state=_clone_state(
                    state,
                    messages=working_messages,
                    transition=RuntimeContinueReason.MAX_OUTPUT_TOKENS_ESCALATE,
                    max_output_tokens_override=ESCALATED_MAX_OUTPUT_TOKENS,
                ),
            )
        if state.max_output_tokens_recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
            continuation_message = TranscriptItem(
                role=RuntimeMessageRole.USER,
                content=CONTINUATION_NUDGE,
                name="max_output_tokens_recovery",
                metadata={"synthetic": True, "kind": "continuation_nudge"},
            )
            return DegradationDecision(
                next_state=_clone_state(
                    state,
                    messages=[*working_messages, continuation_message],
                    transition=RuntimeContinueReason.MAX_OUTPUT_TOKENS_RECOVERY,
                    max_output_tokens_recovery_count=state.max_output_tokens_recovery_count + 1,
                ),
            )
        return DegradationDecision(stop_reason=RuntimeStopReason.COMPLETED)

    if kind == "prompt_too_long" and _has_pending_collapse(state):
        drained_messages, drained_state, committed_count = recover_context_collapse_from_overflow(working_messages, state)
        if committed_count > 0:
            drained_state.transition = RuntimeContinueReason.COLLAPSE_DRAIN_RETRY
            return DegradationDecision(
                next_state=drained_state,
                checkpoint_payload=_checkpoint_recovery_payload(
                    strategy="collapse_drain",
                    status="deferred",
                    committed=committed_count,
                ),
            )

    if kind in {"prompt_too_long", "image_error", "media_size"} and not state.has_attempted_reactive_compact:
        next_tracking = _updated_compact_tracking(
            state,
            strategy="reactive_compact",
            status="deferred",
            pending_collapse=_has_pending_collapse(state),
            reactive_compact_attempted=True,
        )
        reactive_state = _clone_state(
            state,
            messages=working_messages,
            transition=RuntimeContinueReason.REACTIVE_COMPACT_RETRY,
            has_attempted_reactive_compact=True,
            auto_compact_tracking=next_tracking,
        )
        pivot_index = _resolve_reactive_compact_pivot(working_messages, reactive_state)
        try:
            compact_result = await partial_compact_conversation(
                working_messages,
                pivot_index=pivot_index,
                state=reactive_state,
                model=str(model or state.tool_use_context.get("main_loop_model") or "claude-sonnet-4-5"),
                model_client=model_client,
                direction="up_to",
                strategy="reactive_compact",
                boundary_name="reactive_compact_boundary",
                summary_name="reactive_compact_summary",
                recoverable_error_kind=kind,
            )
        except RuntimeError:
            terminal = RuntimeStopReason.IMAGE_ERROR if kind in {"image_error", "media_size"} else RuntimeStopReason.PROMPT_TOO_LONG
            return DegradationDecision(stop_reason=terminal)
        compacted_messages = build_post_compact_messages(compact_result)
        compacted_state = _clone_state(
            reactive_state,
            messages=compacted_messages,
            transition=RuntimeContinueReason.REACTIVE_COMPACT_RETRY,
            has_attempted_reactive_compact=True,
            auto_compact_tracking=_merge_reactive_compact_tracking(reactive_state, compact_result),
        )
        return DegradationDecision(
            next_state=compacted_state,
            checkpoint_payload=_checkpoint_recovery_payload(
                strategy="reactive_compact",
                status="deferred",
                direction="up_to",
                media_stripped_count=int((compact_result.compaction_usage or {}).get("media_stripped_count") or 0),
            ),
        )

    terminal = RuntimeStopReason.IMAGE_ERROR if kind in {"image_error", "media_size"} else RuntimeStopReason.PROMPT_TOO_LONG
    return DegradationDecision(stop_reason=terminal)


def _has_pending_collapse(state: QueryLoopState) -> bool:
    tracking = state.auto_compact_tracking or {}
    if bool(tracking.get("pending_collapse")):
        return True
    snapshot = dict((state.context_collapse_state or {}).get("snapshot") or {})
    return bool(snapshot.get("staged") or [])


def _resolve_reactive_compact_pivot(messages: list[TranscriptItem], state: QueryLoopState) -> int:
    pipeline = dict(state.tool_use_context or {})
    reactive_config = dict(pipeline.get("reactive_compact") or {})
    autocompact_config = dict(pipeline.get("autocompact") or {})
    default_tail = int(autocompact_config.get("preserve_tail_messages") or 2)
    preserve_tail_messages = max(0, int(reactive_config.get("preserve_tail_messages") or default_tail))
    if len(messages) <= 1:
        return len(messages)
    tail_count = min(max(0, preserve_tail_messages), len(messages) - 1)
    if tail_count <= 0:
        return len(messages)
    return len(messages) - tail_count


def _merge_reactive_compact_tracking(state: QueryLoopState, result) -> dict[str, object]:
    usage = dict(result.compaction_usage or {})
    tracking = dict(state.auto_compact_tracking or {})
    tracking.update(
        {
            "compacted": True,
            "summary_message_name": result.summary_messages[0].name if result.summary_messages else None,
            "boundary_name": result.boundary_marker.name,
            "compacted_messages": int(usage.get("messages_summarized") or 0),
            "preserved_tail_messages": len(result.messages_to_keep or []),
            "media_stripped_count": int(usage.get("media_stripped_count") or 0),
            "last_recovery_strategy": "reactive_compact",
            "last_recovery_status": "deferred",
            "reactive_compact_attempted": True,
        }
    )
    return tracking


def _clone_state(
    state: QueryLoopState,
    *,
    messages: list[TranscriptItem],
    transition: RuntimeContinueReason,
    max_output_tokens_override: int | None | object = None,
    max_output_tokens_recovery_count: int | None = None,
    has_attempted_reactive_compact: bool | None = None,
    auto_compact_tracking: dict[str, object] | None | object = None,
    tool_use_context: dict[str, object] | None | object = None,
    context_collapse_state: dict[str, object] | None | object = None,
) -> QueryLoopState:
    override = state.max_output_tokens_override if max_output_tokens_override is None else max_output_tokens_override
    recovery_count = state.max_output_tokens_recovery_count if max_output_tokens_recovery_count is None else max_output_tokens_recovery_count
    reactive_compact = state.has_attempted_reactive_compact if has_attempted_reactive_compact is None else has_attempted_reactive_compact
    compact_tracking = state.auto_compact_tracking if auto_compact_tracking is None else auto_compact_tracking
    next_tool_use_context = state.tool_use_context if tool_use_context is None else tool_use_context
    next_collapse_state = state.context_collapse_state if context_collapse_state is None else context_collapse_state
    return QueryLoopState(
        messages=list(messages),
        tool_use_context=deepcopy(next_tool_use_context),
        auto_compact_tracking=deepcopy(compact_tracking),
        context_collapse_state=deepcopy(next_collapse_state),
        max_output_tokens_recovery_count=recovery_count,
        has_attempted_reactive_compact=reactive_compact,
        max_output_tokens_override=override,
        pending_tool_use_summary=deepcopy(state.pending_tool_use_summary),
        stop_hook_active=state.stop_hook_active,
        turn_count=max(1, state.turn_count),
        transition=transition,
    )


def _checkpoint_recovery_payload(*, strategy: str, status: str, committed: int | None = None, direction: str | None = None, media_stripped_count: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "strategy": strategy,
        "status": status,
    }
    if committed is not None:
        payload["committed"] = committed
    if direction is not None:
        payload["direction"] = direction
    if media_stripped_count is not None:
        payload["media_stripped_count"] = media_stripped_count
    return {"recovery": payload}


def _updated_compact_tracking(
    state: QueryLoopState,
    *,
    strategy: str,
    status: str,
    pending_collapse: bool,
    reactive_compact_attempted: bool | None = None,
) -> dict[str, object]:
    tracking = dict(state.auto_compact_tracking or {})
    tracking["pending_collapse"] = pending_collapse
    tracking["last_recovery_strategy"] = strategy
    tracking["last_recovery_status"] = status
    if reactive_compact_attempted is not None:
        tracking["reactive_compact_attempted"] = reactive_compact_attempted
    return tracking
