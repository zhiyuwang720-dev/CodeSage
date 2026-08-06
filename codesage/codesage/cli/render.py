"""Render SessionMessages to terminal text (Claude Code-style plain-text UI).

Layout conventions (mirroring Claude Code's terminal renderer, simplified
for a pure-text terminal):
- assistant text: streaming, complete-lines only (the tail line is buffered
  until it ends)
- tool calls: one line per tool — `◈ name (summary)`; results are truncated
  to 3 lines with "(ctrl+o to expand)" when the transcript mode is off
- thinking: collapsed to one line unless transcript mode
- transcript mode (Ctrl+O) shows full tool results and thinking
"""

from __future__ import annotations

import sys
from typing import TextIO

from ..core import SessionMessage

#: Minimal ANSI colors (Windows 10+ terminals support these).
RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"

USE_COLOR = sys.stdout.isatty()

#: Collapsed result lines shown before "(ctrl+o to expand)" (CC MAX_LINES=3).
COLLAPSED_RESULT_LINES = 3
RESULT_PREVIEW_CHARS = 400
TOOL_INPUT_SUMMARY_CHARS = 80


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if USE_COLOR else text


def _glyph(glyph: str, out: TextIO) -> str:
    """Fall back to ASCII when the target stream's encoding can't render it."""
    encoding = getattr(out, "encoding", None) or "utf-8"
    try:
        glyph.encode(encoding)
        return glyph
    except (LookupError, UnicodeEncodeError):
        return {"◈": ">>", "✓": "OK", "✗": "ERR", "●": "*", "∴": "...", "⎿": "|"}.get(glyph, "?")


def _collapse(text: str, out: TextIO, max_lines: int = COLLAPSED_RESULT_LINES) -> tuple[str, bool]:
    """Truncate *text* to *max_lines* lines; returns (text, was_truncated)."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    return "\n".join(lines[:max_lines]), True


def render_streamed_text_delta(delta: str, out: TextIO) -> None:
    """Print complete lines of streamed text; the partial tail is buffered
    in the caller (CC: only finished lines are shown)."""
    lines = delta.split("\n")
    for line in lines[:-1]:
        print(line, file=out, flush=True)


def render_message(message: SessionMessage, out: TextIO = sys.stdout, transcript: bool = False) -> None:
    """Render one message. *transcript* = Ctrl+O expanded mode."""
    if message.role == "user":
        _render_user(message, out, transcript)
    else:
        _render_assistant(message, out, transcript)


def _render_user(message: SessionMessage, out: TextIO, transcript: bool) -> None:
    if message.is_compaction_summary:
        # compaction artifact, not user speech: one dim status line
        print(_c("\n[compacted: history summarized]", DIM), file=out)
        return
    content = message.content
    if isinstance(content, str):
        print(f"\n{_c('You:', CYAN)} {content}", file=out)
        return
    for block in content:
        if block.type == "tool_result":
            status = _c(_glyph("✗", out), RED) if block.is_error else _c(_glyph("✓", out), GREEN)
            body = block.content if isinstance(block.content, str) else ""
            body = body.strip()
            if not body:
                print(f"  {status} tool[{_short_id(block.tool_use_id)}] (no output)", file=out)
                continue
            if transcript:
                print(f"  {status} tool[{_short_id(block.tool_use_id)}]", file=out)
                print(_indent(body, 4), file=out)
            else:
                preview, truncated = _collapse(body, out)
                if truncated:
                    print(f"  {status} tool[{_short_id(block.tool_use_id)}]", file=out)
                    print(_indent(preview, 4), file=out)
                    print(_c(f"  … +{len(body.splitlines()) - COLLAPSED_RESULT_LINES} lines (ctrl+o to expand)", DIM), file=out)
                else:
                    print(f"  {status} tool[{_short_id(block.tool_use_id)}] {_summarize(body)}", file=out)


def _render_assistant(message: SessionMessage, out: TextIO, transcript: bool) -> None:
    if message.is_meta:
        print(_c(f"\n[{message.content}]", YELLOW), file=out)
        return
    content = message.content
    if isinstance(content, str):
        print(_c(f"\n{content}", RED) if message.is_error else f"\n{content}", file=out)
        return
    if message.is_error and not _blocks_text(content):
        detail = message.error_message or "unknown provider error"
        print(_c(f"\n(provider error: {detail})", RED), file=out)
        return

    text_parts: list[str] = []
    thinking_chars = 0
    for block in content:
        if block.type == "thinking":
            thinking_chars += len(block.text or "")
        elif block.type == "text":
            text_parts.append(block.text or "")
        elif block.type == "tool_use":
            preview = _summarize_tool_call(block.name or "", block.input or {})
            print(_c(f"\n{_glyph('◈', out)} {preview}", CYAN), file=out)
    if thinking_chars:
        if transcript:
            for block in content:
                if block.type == "thinking" and block.text:
                    print(_c(f"{_glyph('∴', out)} Thinking…", DIM), file=out)
                    print(_indent(block.text, 2), file=out)
        else:
            print(_c(f"  {_glyph('∴', out)} Thinking {thinking_chars} chars (ctrl+o to expand)", DIM), file=out)
    if text_parts:
        print("\n" + "\n".join(text_parts), file=out)


def _indent(text: str, width: int) -> str:
    return "\n".join(" " * width + line for line in text.splitlines())


def _summarize_tool_call(name: str, input: dict) -> str:
    bits = [f"{k}={str(v)[:TOOL_INPUT_SUMMARY_CHARS]}" for k, v in input.items()]
    return f"{name} {', '.join(bits)[:200]}"


def _summarize(text: str) -> str:
    text = " ".join(text.split())
    return text[:RESULT_PREVIEW_CHARS] + ("…" if len(text) > RESULT_PREVIEW_CHARS else "")


def _short_id(tool_use_id: str | None) -> str:
    return (tool_use_id or "")[-6:] or "?"


def _blocks_text(content) -> bool:
    return any(b.type == "text" and b.text for b in content)
