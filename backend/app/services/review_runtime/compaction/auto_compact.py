from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from app.services.review_runtime.compaction.compact import compact_conversation
from app.services.review_runtime.compaction.models import AutoCompactTrackingState
from app.services.contracts.models import TranscriptItem
from app.services.contracts.query_state import QueryLoopState

MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3


@dataclass(slots=True)
class AutoCompactDecision:
    was_compacted: bool
    compaction_result: Any | None = None
    consecutive_failures: int | None = None


def get_effective_context_window_size(*, model: str, context_window: int, max_output_tokens: int) -> int:
    del model
    reserved_tokens = min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return max(0, context_window - reserved_tokens)


def get_auto_compact_threshold(*, model: str, context_window: int, max_output_tokens: int) -> int:
    effective_window = get_effective_context_window_size(
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
    return max(0, effective_window - AUTOCOMPACT_BUFFER_TOKENS)


def calculate_token_warning_state(*, token_usage: int, model: str, context_window: int, max_output_tokens: int) -> dict[str, Any]:
    auto_compact_threshold = get_auto_compact_threshold(
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
    effective_window = get_effective_context_window_size(
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
    threshold = auto_compact_threshold
    percent_left = max(0, round(((threshold - token_usage) / threshold) * 100)) if threshold > 0 else 0
    warning_threshold = threshold - WARNING_THRESHOLD_BUFFER_TOKENS
    error_threshold = threshold - ERROR_THRESHOLD_BUFFER_TOKENS
    blocking_limit = effective_window - MANUAL_COMPACT_BUFFER_TOKENS
    return {
        "percent_left": percent_left,
        "is_above_warning_threshold": token_usage >= warning_threshold,
        "is_above_error_threshold": token_usage >= error_threshold,
        "is_above_auto_compact_threshold": token_usage >= auto_compact_threshold,
        "is_at_blocking_limit": token_usage >= blocking_limit,
    }


def auto_compact_if_needed(
    messages: list[TranscriptItem],
    state: QueryLoopState,
    *,
    tracking: AutoCompactTrackingState | None,
    compactor: Callable[..., Any] | None = None,
    model: str = "claude-sonnet-4-5",
) -> AutoCompactDecision:
    if tracking is not None and (tracking.consecutive_failures or 0) >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        return AutoCompactDecision(
            was_compacted=False,
            consecutive_failures=tracking.consecutive_failures,
        )

    controller = dict(state.tool_use_context.get("autocompact_controller") or {})
    context_window = int(controller.get("context_window") or 0)
    max_output_tokens = int(controller.get("max_output_tokens") or 0)
    token_usage = sum(len(message.content or "") for message in messages)
    threshold = get_auto_compact_threshold(
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
    if threshold <= 0 or token_usage < threshold:
        return AutoCompactDecision(
            was_compacted=False,
            consecutive_failures=(tracking.consecutive_failures if tracking is not None else None),
        )

    chosen_compactor = compactor or compact_conversation

    async def _run_async() -> AutoCompactDecision:
        try:
            result = chosen_compactor(
                messages,
                state,
                tracking=tracking,
                model=model,
                token_usage=token_usage,
                auto_compact_threshold=threshold,
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            previous = tracking.consecutive_failures if tracking is not None and tracking.consecutive_failures is not None else 0
            return AutoCompactDecision(
                was_compacted=False,
                consecutive_failures=previous + 1,
            )

        return AutoCompactDecision(
            was_compacted=True,
            compaction_result=result,
            consecutive_failures=0,
        )

    coroutine = _run_async()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    return coroutine
