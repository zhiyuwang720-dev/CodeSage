"""Normalize conversation history for the API (Kode normalizeMessagesForAPI).

Rules (mirroring Kode's message-utils/api.ts):
1. Drop is_error / is_meta messages (provider errors, synthesized notices).
2. Whitespace-only text blocks are dropped; a message left empty gets the
   "(no content)" sentinel (Kode NO_CONTENT_MESSAGE).
3. Adjacent same-role messages merge into one. Merged user content is
   reordered toolResultsFirst — all tool_result blocks precede text — so one
   user message may carry [tool_result..., text...] blocks (Anthropic-valid
   inline format). Kode anchors assistant merges on message.id; here adjacent
   assistants simply concatenate.
"""

from __future__ import annotations

from typing import Any

from ..ai import ContentBlock, Message

#: Sentinel for messages whose content normalized away (Kode NO_CONTENT_MESSAGE).
NO_CONTENT_MESSAGE = "(no content)"


def _blocks(content: str | list[ContentBlock]) -> list[ContentBlock]:
    if isinstance(content, str):
        return [ContentBlock(type="text", text=content)]
    return content


def _clean_content(content: str | list[ContentBlock]) -> str | list[ContentBlock]:
    """Drop whitespace-only text blocks; empty content becomes the sentinel."""
    if isinstance(content, str):
        return content if content.strip() else NO_CONTENT_MESSAGE
    blocks = [b for b in content if b.type != "text" or (b.text or "").strip()]
    return blocks if blocks else NO_CONTENT_MESSAGE


def _tool_results_first(blocks: list[ContentBlock]) -> list[ContentBlock]:
    tool_results = [b for b in blocks if b.type == "tool_result"]
    rest = [b for b in blocks if b.type != "tool_result"]
    return tool_results + rest


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
        if getattr(msg, "is_error", False) or getattr(msg, "is_meta", False):
            continue
        content = _clean_content(getattr(msg, "content"))

        prev = out[-1] if out else None
        if prev is not None and prev.role == role:
            merged = _merge_content(prev.content, content)
            if role == "user":
                blocks = _blocks(merged)
                if any(b.type == "tool_result" for b in blocks):
                    merged = _tool_results_first(blocks)  # reorder only, never convert
            prev.content = merged
        else:
            out.append(Message(role=role, content=content))
    return out
