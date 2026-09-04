from __future__ import annotations

from copy import deepcopy

from app.services.contracts.models import RuntimeContinueReason, TranscriptItem
from app.services.contracts.query_state import QueryLoopState


def hydrate_query_loop_state(state: QueryLoopState, *, messages: list[TranscriptItem]) -> QueryLoopState:
    return QueryLoopState(
        messages=list(messages),
        tool_use_context=deepcopy(state.tool_use_context),
        auto_compact_tracking=deepcopy(state.auto_compact_tracking),
        context_collapse_state=deepcopy(state.context_collapse_state),
        max_output_tokens_recovery_count=state.max_output_tokens_recovery_count,
        has_attempted_reactive_compact=state.has_attempted_reactive_compact,
        max_output_tokens_override=state.max_output_tokens_override,
        pending_tool_use_summary=deepcopy(state.pending_tool_use_summary),
        stop_hook_active=state.stop_hook_active,
        turn_count=max(1, state.turn_count),
        transition=state.transition,
    )


def build_continue_state(
    state: QueryLoopState,
    *,
    messages: list[TranscriptItem],
    transition: RuntimeContinueReason,
) -> QueryLoopState:
    return QueryLoopState(
        messages=list(messages),
        tool_use_context=deepcopy(state.tool_use_context),
        auto_compact_tracking=deepcopy(state.auto_compact_tracking),
        context_collapse_state=deepcopy(state.context_collapse_state),
        max_output_tokens_recovery_count=state.max_output_tokens_recovery_count,
        has_attempted_reactive_compact=state.has_attempted_reactive_compact,
        max_output_tokens_override=state.max_output_tokens_override,
        pending_tool_use_summary=deepcopy(state.pending_tool_use_summary),
        stop_hook_active=state.stop_hook_active,
        turn_count=max(1, state.turn_count) + 1,
        transition=transition,
    )


def build_terminal_state(state: QueryLoopState, *, messages: list[TranscriptItem]) -> QueryLoopState:
    return QueryLoopState(
        messages=list(messages),
        tool_use_context=deepcopy(state.tool_use_context),
        auto_compact_tracking=deepcopy(state.auto_compact_tracking),
        context_collapse_state=deepcopy(state.context_collapse_state),
        max_output_tokens_recovery_count=state.max_output_tokens_recovery_count,
        has_attempted_reactive_compact=state.has_attempted_reactive_compact,
        max_output_tokens_override=state.max_output_tokens_override,
        pending_tool_use_summary=deepcopy(state.pending_tool_use_summary),
        stop_hook_active=state.stop_hook_active,
        turn_count=max(1, state.turn_count),
        transition=None,
    )
