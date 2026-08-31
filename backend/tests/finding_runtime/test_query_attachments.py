from __future__ import annotations

from app.services.review_runtime.models import ToolCallRequest, ToolExecutionPayload, ToolExecutionRecord
from app.services.review_runtime.query_attachments import build_between_turn_attachments, start_pending_tool_use_summary
from app.services.review_runtime.query_state import QueryLoopState


def _record(name: str, content: str, *, status: str = "completed", is_error: bool = False) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        tool_call_id=f"call-{name}",
        request=ToolCallRequest(id=f"tool-{name}", name=name, input={"path": f"/tmp/{name}"}),
        status=status,
        is_concurrency_safe=True,
        result=ToolExecutionPayload(
            content=content,
            output_payload={"content": content},
            is_error=is_error,
        ),
        error_message=content if is_error else None,
        duration_ms=12,
    )


def test_build_between_turn_attachments_emits_tool_attachment_summary():
    attachments = build_between_turn_attachments(
        state=QueryLoopState(tool_use_context={"native_tool_history": False}),
        records=[_record("echo", "repo summary"), _record("grep", "match lines")],
        session_snapshot=None,
    )

    assert len(attachments) == 1
    assert attachments[0].name == "tool_results_attachment"
    assert "echo(completed)" in attachments[0].content
    assert "grep(completed)" in attachments[0].content


def test_start_pending_tool_use_summary_returns_ready_summary_payload():
    summary = start_pending_tool_use_summary(
        state=QueryLoopState(tool_use_context={"native_tool_history": False}),
        records=[_record("echo", "repo summary"), _record("grep", "match lines", status="failed", is_error=True)],
        session_snapshot=None,
    )

    assert summary is not None
    assert summary["status"] == "ready"
    assert summary["tool_names"] == ["echo", "grep"]
    assert "Tool-use summary" in summary["summary_message"]
    assert "grep: failed" in summary["summary_message"]


def test_tool_attachments_are_hidden_when_native_tool_history_is_enabled():
    state = QueryLoopState(tool_use_context={"native_tool_history": True})

    assert build_between_turn_attachments(
        state=state,
        records=[_record("echo", "repo summary")],
        session_snapshot=None,
    ) == []
    assert start_pending_tool_use_summary(
        state=state,
        records=[_record("echo", "repo summary")],
        session_snapshot=None,
    ) is None
