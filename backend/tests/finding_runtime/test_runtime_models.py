from app.services.runtime.config import RuntimeStack, coerce_runtime_stack
from app.services.contracts.models import (
    RuntimeContinueReason,
    RuntimeMessageRole,
    RuntimeSessionState,
    RuntimeStopReason,
    TurnExecutionResult,
    ToolCallRequest,
    TranscriptItem,
)


def test_runtime_stack_defaults_to_legacy_for_unknown_values():
    assert coerce_runtime_stack(None) is RuntimeStack.LEGACY
    assert coerce_runtime_stack("") is RuntimeStack.LEGACY
    assert coerce_runtime_stack("something-else") is RuntimeStack.LEGACY


def test_runtime_stack_accepts_phase_one_runtime_flag_values():
    assert coerce_runtime_stack("runtime") is RuntimeStack.RUNTIME
    assert coerce_runtime_stack("new") is RuntimeStack.RUNTIME
    assert coerce_runtime_stack("legacy") is RuntimeStack.LEGACY


def test_runtime_enums_expose_runtime_states_and_stop_reasons():
    assert RuntimeSessionState.PENDING.value == "pending"
    assert RuntimeSessionState.RUNNING.value == "running"
    assert RuntimeSessionState.COMPLETED.value == "completed"

    assert RuntimeStopReason.COMPLETED.value == "completed"
    assert RuntimeStopReason.BLOCKING_LIMIT.value == "blocking_limit"
    assert RuntimeStopReason.PROMPT_TOO_LONG.value == "prompt_too_long"
    assert RuntimeStopReason.IMAGE_ERROR.value == "image_error"
    assert RuntimeStopReason.MODEL_ERROR.value == "model_error"
    assert RuntimeStopReason.PERSISTENCE_ERROR.value == "persistence_error"
    assert RuntimeStopReason.ABORTED_STREAMING.value == "aborted_streaming"
    assert RuntimeStopReason.ABORTED_TOOLS.value == "aborted_tools"
    assert RuntimeStopReason.STOP_HOOK_PREVENTED.value == "stop_hook_prevented"
    assert RuntimeStopReason.HOOK_STOPPED.value == "hook_stopped"
    assert RuntimeStopReason.MAX_TURNS.value == "max_turns"

    assert RuntimeContinueReason.NEXT_TURN.value == "next_turn"
    assert RuntimeContinueReason.MAX_OUTPUT_TOKENS_ESCALATE.value == "max_output_tokens_escalate"
    assert RuntimeContinueReason.MAX_OUTPUT_TOKENS_RECOVERY.value == "max_output_tokens_recovery"
    assert RuntimeContinueReason.REACTIVE_COMPACT_RETRY.value == "reactive_compact_retry"
    assert RuntimeContinueReason.COLLAPSE_DRAIN_RETRY.value == "collapse_drain_retry"
    assert RuntimeContinueReason.STOP_HOOK_BLOCKING.value == "stop_hook_blocking"
    assert RuntimeContinueReason.TOKEN_BUDGET_CONTINUATION.value == "token_budget_continuation"


def test_turn_execution_result_supports_transition_reasons():
    result = TurnExecutionResult(
        turn_id="turn-1",
        stop_reason=None,
        transition=RuntimeContinueReason.NEXT_TURN,
    )

    assert result.turn_id == "turn-1"
    assert result.stop_reason is None
    assert result.transition is RuntimeContinueReason.NEXT_TURN


def test_transcript_item_normalizes_optional_metadata_and_payloads():
    item = TranscriptItem(
        role=RuntimeMessageRole.ASSISTANT,
        content="hello world",
    )

    assert item.role is RuntimeMessageRole.ASSISTANT
    assert item.content == "hello world"
    assert item.name is None
    assert item.metadata == {}
    assert item.payload == {}


def test_tool_call_request_defaults_input_payload_to_empty_mapping():
    call = ToolCallRequest(id="tool-1", name="echo")

    assert call.id == "tool-1"
    assert call.name == "echo"
    assert call.input == {}
