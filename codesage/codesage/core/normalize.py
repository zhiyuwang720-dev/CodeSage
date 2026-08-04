"""Normalize conversation history for the API (Kode normalizeMessagesForAPI).

Rules (mirroring Kode's message-utils/api.ts):
1. Drop is_error / is_meta messages (provider errors, synthesized notices).
2. tool_result blocks are split into their own user message (Anthropic
   semantics); any sibling text becomes a separate user message.
3. Adjacent same-role messages merge (their content concatenates).
   tool_result-carrying messages never merge with plain text (provider
   wire formats distinguish them).
"""

from __future__ import annotations

from typing import Any

from ..ai import ContentBlock, Message


def _blocks(content: str | list[ContentBlock]) -> list[ContentBlock]:
    if isinstance(content, str):
        return [ContentBlock(type="text", text=content)]
    return content


def _carries_tool_result(content: str | list[ContentBlock]) -> bool:
    return isinstance(content, list) and any(b.type == "tool_result" for b in content)


def _merge_content(prev: str | list[ContentBlock], cur: str | list[ContentBlock]) -> str | list[ContentBlock]:
    """Concatenate two same-role contents (both str, or both block lists)."""
    if isinstance(prev, str) and isinstance(cur, str):
        return prev + "\n" + cur
    return _blocks(prev) + _blocks(cur)


def normalize_for_api(messages: list[Any]) -> list[Message]:
    """Convert session history into API-ready messages.

    Accepts any object exposing .role/.content/.is_error/.is_meta (SessionMessage
    or the plain ai.Message); returns plain ai.Message list.
    """
    out: list[Message] = []
    for msg in messages:
        role = getattr(msg, "role")
        content = getattr(msg, "content")
        if getattr(msg, "is_error", False) or getattr(msg, "is_meta", False):
            continue

        if _carries_tool_result(content):
            blocks = _blocks(content)
            text_blocks = [b for b in blocks if b.type != "tool_result"]
            tool_blocks = [b for b in blocks if b.type == "tool_result"]
            if text_blocks:
                out.append(Message(role="user", content=text_blocks))
            out.append(Message(role="user", content=tool_blocks))
            continue

        prev = out[-1] if out else None
        if prev is not None and prev.role == role and not _carries_tool_result(prev.content):
            prev.content = _merge_content(prev.content, content)
        else:
            out.append(Message(role=role, content=content))
    return out
