"""Render tests: message → terminal text."""

import io

from codesage.ai import ContentBlock
from codesage.core import assistant_message, user_message
from codesage.cli.render import render_message


def _render(msg, **kw):
    buf = io.StringIO()
    transcript = kw.pop("transcript", kw.pop("show_thinking", False))
    render_message(msg, out=buf, transcript=transcript)
    return buf.getvalue()


def test_user_text():
    out = _render(user_message("hello"))
    assert "hello" in out


def test_tool_result_round():
    msg = user_message([ContentBlock(type="tool_result", tool_use_id="t123456", content="result text")])
    out = _render(msg)
    assert "✓" in out
    assert "result text" in out


def test_tool_result_error():
    msg = user_message([ContentBlock(type="tool_result", tool_use_id="t1", content="boom", is_error=True)])
    assert "✗" in _render(msg)


def test_assistant_text_and_thinking():
    msg = assistant_message([ContentBlock(type="thinking", text="secret reasoning"), ContentBlock(type="text", text="answer")])
    out = _render(msg, show_thinking=False)
    assert "answer" in out
    assert "secret reasoning" not in out  # thinking hidden by default
    assert "Thinking" in out  # but its length is shown


def test_assistant_show_thinking():
    msg = assistant_message([ContentBlock(type="thinking", text="visible"), ContentBlock(type="text", text="answer")])
    out = _render(msg, transcript=True)
    assert "visible" in out


def test_tool_use_call_line():
    msg = assistant_message([ContentBlock(type="tool_use", id="t1", name="Read", input={"file_path": "/x"})])
    out = _render(msg)
    assert "Read" in out and "file_path" in out


def test_meta_message():
    msg = assistant_message("(interrupted)", is_meta=True)
    assert "(interrupted)" in _render(msg)


def test_result_preview_truncated():
    msg = user_message([ContentBlock(type="tool_result", tool_use_id="t1", content="x" * 1000)])
    out = _render(msg)
    assert "…" in out


def test_midrun_tool_lines_are_grey(monkeypatch):
    """Agent mid-run artifacts (tool calls/results/thinking) render grey;
    the final assistant text stays uncolored (spec: grey = intermediate)."""
    monkeypatch.setattr("codesage.cli.render.USE_COLOR", True)
    from codesage.ai import ContentBlock
    from codesage.core import assistant_message, user_message

    mid = assistant_message(
        [
            ContentBlock(type="thinking", text="hmm"),
            ContentBlock(type="tool_use", id="t1", name="Read", input={"file_path": "x.py"}),
        ]
    )
    out = _render(mid, transcript=True)
    assert "\033[90m" in out  # tool_use line + thinking grey

    result = user_message(
        [ContentBlock(type="tool_result", tool_use_id="t1", content="file content", is_error=False)]
    )
    out = _render(result)
    assert "\033[90m" in out  # tool_result line grey
    assert "✓" in out  # glyph still distinguishable

    final = assistant_message("the final answer")
    out = _render(final)
    assert "the final answer" in out
    assert "\033[90m" not in out  # final text uncolored


def test_truncated_output_shows_hint():
    """stop_reason=length surfaces a truncation hint instead of looking cut off."""
    from codesage.core import assistant_message

    msg = assistant_message("partial answer", stop_reason="length")
    out = _render(msg)
    assert "output truncated" in out
    # normal replies carry no hint
    assert "output truncated" not in _render(assistant_message("full answer"))
