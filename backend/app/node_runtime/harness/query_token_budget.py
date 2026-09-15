from __future__ import annotations

from typing import Any


DEFAULT_TOKEN_BUDGET_NUDGE = "Continue investigating until you either exhaust credible attack paths or produce stronger evidence for a reportable finding."


def evaluate_token_budget_continuation(*, runtime_state: Any, state: Any, model_response: Any) -> dict[str, Any]:
    token_budget = dict(getattr(runtime_state, "metadata", {}).get("token_budget") or {})
    if "should_continue" in token_budget:
        should_continue = bool(token_budget.get("should_continue") or False)
        message = str(token_budget.get("message") or DEFAULT_TOKEN_BUDGET_NUDGE).strip()
        return {
            "should_continue": should_continue,
            "message": message,
        }

    budget_chars = int(token_budget.get("budget_chars") or 0)
    minimum_remaining_chars = int(token_budget.get("minimum_remaining_chars") or 0)
    max_turns = int(token_budget.get("max_turns") or 0)

    current_chars = 0
    for message in getattr(state, "messages", []) or []:
        current_chars += len(str(getattr(message, "content", "") or ""))
    current_chars += len(str(getattr(model_response, "content", "") or ""))

    remaining_chars = budget_chars - current_chars
    blocked_by_turn_limit = bool(max_turns and int(getattr(state, "turn_count", 0) or 0) >= max_turns)
    should_continue = bool(budget_chars and remaining_chars > minimum_remaining_chars and not blocked_by_turn_limit)

    configured_message = str(token_budget.get("message") or "").strip()
    if configured_message:
        message = configured_message
    elif should_continue:
        message = f"Continue investigating while remaining budget is available ({remaining_chars} chars left)."
    else:
        message = DEFAULT_TOKEN_BUDGET_NUDGE

    return {
        "should_continue": should_continue,
        "message": message,
        "remaining_chars": remaining_chars,
        "blocked_by_turn_limit": blocked_by_turn_limit,
    }
