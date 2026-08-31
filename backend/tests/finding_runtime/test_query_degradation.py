from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.services.review_runtime.models import RuntimeContinueReason, RuntimeMessageRole, RuntimeModelResponse, RuntimeStopReason, TranscriptItem
from app.services.review_runtime.query_degradation import handle_recoverable_response
from app.services.review_runtime.query_state import QueryLoopState

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "query_parity" / "degradation_cases.json"
CASES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_restored_inspired_degradation_cases(case):
    state = QueryLoopState(
        messages=[TranscriptItem(role=RuntimeMessageRole.USER, content="inspect code")],
        auto_compact_tracking=case.get("state", {}).get("auto_compact_tracking"),
        max_output_tokens_recovery_count=case.get("state", {}).get("max_output_tokens_recovery_count", 0),
        has_attempted_reactive_compact=case.get("state", {}).get("has_attempted_reactive_compact", False),
        max_output_tokens_override=case.get("state", {}).get("max_output_tokens_override"),
    )
    decision = asyncio.run(handle_recoverable_response(
        state=state,
        working_messages=list(state.messages),
        model_response=RuntimeModelResponse(
            content="partial answer",
            recoverable_error_kind=case["recoverable_error_kind"],
            recoverable_error_message="fixture-driven parity scenario",
        ),
    ))

    assert decision is not None
    expected_transition = case.get("expected_transition")
    expected_stop_reason = case.get("expected_stop_reason")

    if expected_transition is not None:
        assert decision.next_state is not None
        assert decision.next_state.transition is RuntimeContinueReason(expected_transition)
        assert decision.stop_reason is None
    if expected_stop_reason is not None:
        assert decision.stop_reason is RuntimeStopReason(expected_stop_reason)
        assert decision.next_state is None

    if "expected_override" in case:
        assert decision.next_state is not None
        assert decision.next_state.max_output_tokens_override == case["expected_override"]
    if "expected_recovery_count" in case:
        assert decision.next_state is not None
        assert decision.next_state.max_output_tokens_recovery_count == case["expected_recovery_count"]
    if "expected_last_message_contains" in case:
        assert decision.next_state is not None
        assert case["expected_last_message_contains"] in decision.next_state.messages[-1].content
    if "expected_has_attempted_reactive_compact" in case:
        assert decision.next_state is not None
        assert decision.next_state.has_attempted_reactive_compact is case["expected_has_attempted_reactive_compact"]
    if "expected_compact_tracking" in case:
        assert decision.next_state is not None
        for key, value in case["expected_compact_tracking"].items():
            assert decision.next_state.auto_compact_tracking[key] == value
    if "expected_checkpoint_recovery" in case:
        assert decision.checkpoint_payload is not None
        recovery = dict(decision.checkpoint_payload.get("recovery") or {})
        for key, value in case["expected_checkpoint_recovery"].items():
            assert recovery[key] == value


def test_collapse_drain_retry_commits_staged_queue_and_projects_committed_view():
    state = QueryLoopState(
        messages=[TranscriptItem(role=RuntimeMessageRole.USER, content="m1")],
        auto_compact_tracking={"pending_collapse": True},
        context_collapse_state={
            "commits": [],
            "snapshot": {
                "staged": [
                    {
                        "start_uuid": "u1",
                        "end_uuid": "u2",
                        "summary": "Collapsed earlier context.",
                        "risk": 5,
                        "staged_at": 1,
                    }
                ],
                "armed": True,
                "last_spawn_tokens": 123,
            },
        },
    )
    working_messages = [
        TranscriptItem(role=RuntimeMessageRole.USER, content="m1", metadata={"collapse_uuid": "u1"}),
        TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="m2", metadata={"collapse_uuid": "u2"}),
        TranscriptItem(role=RuntimeMessageRole.USER, content="tail", metadata={"collapse_uuid": "u3"}),
    ]

    decision = asyncio.run(handle_recoverable_response(
        state=state,
        working_messages=working_messages,
        model_response=RuntimeModelResponse(recoverable_error_kind="prompt_too_long"),
    ))

    assert decision is not None
    assert decision.next_state is not None
    assert decision.next_state.messages[0].name == "context_collapse_summary"
    assert decision.next_state.messages[-1].content == "tail"
    assert len(decision.next_state.context_collapse_state["commits"]) == 1
    assert decision.next_state.context_collapse_state["snapshot"]["staged"] == []
    assert decision.checkpoint_payload == {"recovery": {"strategy": "collapse_drain", "status": "deferred", "committed": 1}}





def test_reactive_compact_retry_uses_partial_compaction_service(monkeypatch):
    called = {}

    async def fake_partial_compact_conversation(all_messages, pivot_index, state, model, model_client=None, user_feedback=None, direction="from", **kwargs):
        called["pivot_index"] = pivot_index
        called["direction"] = direction
        called["messages"] = [item.content for item in all_messages]
        return __import__("app.services.review_runtime.compaction.models", fromlist=["CompactionResult"]).CompactionResult(
            boundary_marker=TranscriptItem(role=RuntimeMessageRole.SYSTEM, content="Reactive compact boundary.", name="reactive_compact_boundary"),
            summary_messages=[TranscriptItem(role=RuntimeMessageRole.USER, content="summary", name="reactive_compact_summary")],
            messages_to_keep=[TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="tail", name="tail")],
        )

    monkeypatch.setattr(
        "app.services.review_runtime.query_degradation.partial_compact_conversation",
        fake_partial_compact_conversation,
    )

    state = QueryLoopState(
        messages=[TranscriptItem(role=RuntimeMessageRole.USER, content="A" * 50)],
        tool_use_context={"reactive_compact": {"preserve_tail_messages": 1}, "main_loop_model": "claude-sonnet-4-5"},
    )
    working_messages = [
        TranscriptItem(role=RuntimeMessageRole.USER, content="older"),
        TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="tail"),
    ]

    decision = asyncio.run(handle_recoverable_response(
        state=state,
        working_messages=working_messages,
        model_response=RuntimeModelResponse(recoverable_error_kind="prompt_too_long"),
    ))

    assert decision is not None
    assert decision.next_state is not None
    assert decision.next_state.messages[0].name == "reactive_compact_boundary"
    assert decision.next_state.messages[-1].content == "tail"
    assert called["direction"] == "up_to"
    assert called["pivot_index"] == 1

def test_reactive_compact_retry_rewrites_next_state_messages_with_restored_style_compact_output():
    state = QueryLoopState(
        messages=[TranscriptItem(role=RuntimeMessageRole.USER, content="A" * 50)],
        tool_use_context={"reactive_compact": {"preserve_tail_messages": 1}},
    )
    working_messages = [
        TranscriptItem(role=RuntimeMessageRole.USER, content="A" * 50),
        TranscriptItem(role=RuntimeMessageRole.ASSISTANT, content="tail"),
    ]

    decision = asyncio.run(handle_recoverable_response(
        state=state,
        working_messages=working_messages,
        model_response=RuntimeModelResponse(recoverable_error_kind="prompt_too_long"),
    ))

    assert decision is not None
    assert decision.next_state is not None
    assert decision.next_state.messages[0].name == "reactive_compact_boundary"
    assert decision.next_state.messages[1].name == "reactive_compact_summary"
    assert decision.next_state.messages[-1].content == "tail"
