"""Audit sink tests: one event per decision, durable, content complete."""

import pytest

from codesage.permissions import JsonlAuditSink, PermissionEngine


def test_every_decision_emits_event(tmp_path):
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    engine = PermissionEngine(audit_sink=sink)
    engine.evaluate_tool_use(tool_name="Bash", tool_input={"command": "ls"})
    engine.evaluate_tool_use(tool_name="Bash", tool_input={"command": "rm -rf x"}, permissions={"deny": ["Bash"]})
    engine.evaluate_tool_use(tool_name="Read", tool_input={"file_path": "/x"}, mode="yolo")

    events = sink.load()
    assert len(events) == 3
    assert events[0]["decision"] == "ask"
    assert events[1]["decision"] == "deny"
    assert events[1]["source"] == "Bash"  # the matched rule
    assert events[1]["reason"].startswith("denied by rule")
    assert events[2]["decision"] == "allow" and events[2]["mode"] == "yolo"


def test_audit_input_summary_never_contains_content(tmp_path):
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    engine = PermissionEngine(audit_sink=sink)
    engine.evaluate_tool_use(
        tool_name="Write",
        tool_input={"file_path": "/x.py", "content": "SECRET_CONTENT_123"},
    )
    event = sink.load()[0]
    assert "SECRET_CONTENT_123" not in str(event)
    assert event["input_summary"]["file_path"] == "/x.py"


def test_jsonl_roundtrip_preserves_fields(tmp_path):
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    from codesage.permissions.audit import ToolAuditEvent

    sink.emit(ToolAuditEvent(tool_name="Bash", decision="deny", source="test", mode="yolo"))
    event = sink.load()[0]
    assert event["tool_name"] == "Bash"
    assert event["timestamp"]  # auto-stamped
