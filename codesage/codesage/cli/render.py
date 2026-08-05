"""Render SessionMessages to terminal text (plain, no UI framework)."""

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

#: Toggle off when output is piped (no ANSI in logs/files).
USE_COLOR = sys.stdout.isatty()

RESULT_PREVIEW_CHARS = 400
TOOL_INPUT_SUMMARY_CHARS = 80


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if USE_COLOR else text


def render_message(message: SessionMessage, out: TextIO | None = None, show_thinking: bool = False) -> None:
    """Render one message to *out* (current sys.stdout when omitted)."""
    if out is None:
        out = sys.stdout
    if message.role == "user":
        _render_user(message, out)
    else:
        _render_assistant(message, out, show_thinking)


def _render_user(message: SessionMessage, out: TextIO) -> None:
    content = message.content
    if isinstance(content, str):
        print(f"\n{_c('You:', CYAN)} {content}", file=out)
        return
    # tool_result round
    for block in content:
        if block.type == "tool_result":
            glyphs = _glyphs(out)
            status = _c(glyphs["err"], RED) if block.is_error else _c(glyphs["ok"], GREEN)
            preview = _summarize_result(block.content, out)
            print(f"  {status} tool[{_short_id(block.tool_use_id)}] {preview}", file=out)


def _render_assistant(message: SessionMessage, out: TextIO, show_thinking: bool) -> None:
    if message.is_meta:
        print(_c(f"\n[{message.content}]", YELLOW), file=out)
        return
    content = message.content
    if isinstance(content, str):
        print(_c(f"\n{content}", RED) if message.is_error else f"\n{content}", file=out)
        return
    if message.is_error and not _blocks_text(content):
        print(_c("\n(provider error)", RED), file=out)
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
            print(_c(f"\n{_glyphs(out)['tool_use']} {preview}", CYAN), file=out)
    if thinking_chars:
        if show_thinking:
            for block in content:
                if block.type == "thinking":
                    print(_c(block.text or "", DIM), file=out)
        else:
            print(_c(f"  (thinking: {thinking_chars} chars)", DIM), file=out)
    if text_parts:
        print("\n" + "\n".join(text_parts), file=out)


def _summarize_tool_call(name: str, input: dict) -> str:
    bits = [f"{k}={str(v)[:TOOL_INPUT_SUMMARY_CHARS]}" for k, v in input.items()]
    return f"{name} {', '.join(bits)[:200]}"


def _summarize_result(content, out: TextIO) -> str:
    text = content if isinstance(content, str) else ""
    if not text:
        return "(no output)"
    text = " ".join(text.split())
    return text[:RESULT_PREVIEW_CHARS] + (_glyphs(out)["ellipsis"] if len(text) > RESULT_PREVIEW_CHARS else "")


def _glyphs(out: TextIO) -> dict[str, str]:
    """Decorative glyphs, ASCII fallbacks when the stream's encoding can't
    represent them (e.g. GBK consoles) — rendering must never crash."""
    encoding = getattr(out, "encoding", None) or "utf-8"
    try:
        "◈✓✗…".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return {"tool_use": ">", "ok": "OK", "err": "ERR", "ellipsis": "..."}
    return {"tool_use": "◈", "ok": "✓", "err": "✗", "ellipsis": "…"}


def _short_id(tool_use_id: str | None) -> str:
    return (tool_use_id or "")[-6:] or "?"


def _blocks_text(content) -> bool:
    return any(b.type == "text" and b.text for b in content)
