from __future__ import annotations

from types import SimpleNamespace

from app.services.contracts.models import RuntimeMessageRole, RuntimeModelResponse, TranscriptItem
from app.services.contracts.query_state import QueryLoopState
from app.services.runtime.query_token_budget import evaluate_token_budget_continuation


def test_evaluate_token_budget_continuation_uses_remaining_budget_and_turn_count():
    runtime_state = SimpleNamespace(
        metadata={
            "token_budget": {
                "budget_chars": 200,
                "minimum_remaining_chars": 40,
                "max_turns": 4,
            }
        }
    )
    state = QueryLoopState(
        messages=[TranscriptItem(role=RuntimeMessageRole.USER, content="inspect code")],
        turn_count=2,
    )
    model_response = RuntimeModelResponse(content="short answer")

    decision = evaluate_token_budget_continuation(
        runtime_state=runtime_state,
        state=state,
        model_response=model_response,
    )

    assert decision["should_continue"] is True
    assert decision["remaining_chars"] > 40
    assert "remaining budget" in decision["message"]


def test_evaluate_token_budget_continuation_stops_when_turn_budget_exhausted():
    runtime_state = SimpleNamespace(
        metadata={
            "token_budget": {
                "budget_chars": 80,
                "minimum_remaining_chars": 20,
                "max_turns": 2,
            }
        }
    )
    state = QueryLoopState(
        messages=[TranscriptItem(role=RuntimeMessageRole.USER, content="A" * 70)],
        turn_count=2,
    )
    model_response = RuntimeModelResponse(content="B" * 30)

    decision = evaluate_token_budget_continuation(
        runtime_state=runtime_state,
        state=state,
        model_response=model_response,
    )

    assert decision["should_continue"] is False
    assert decision["remaining_chars"] <= 0 or decision["blocked_by_turn_limit"] is True
