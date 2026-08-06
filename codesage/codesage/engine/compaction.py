"""Compaction core (phase 08, specs/08 §3.5 — PI-05).

The trigger lives in the loop (S6, turn-top checkpoint); this module owns
the three mechanical pieces: finding the cut point, serializing the
compressed span, and generating the structured summary.

Cut semantics (pi findCutPoint): legal cut points are user / assistant /
summary message boundaries — a tool_result carrier is never a cut point
(turn pairing must stay intact). A cut on a user message keeps the whole
turn in the retained span; a cut on an assistant message splits the turn
and the turn's lead-in (its user input + tool round-trips up to the cut)
gets its own summary so the retained assistant reply still reads as a
response to something.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..ai import ContentBlock, LLMClient, LLMError, LLMRequest, Message
from ..core import SessionMessage, user_message
from .tokens import DEFAULT_RESERVE_TOKENS, estimate_message_tokens

#: Recent tokens kept intact at the end of the conversation (pi keepRecentTokens).
DEFAULT_KEEP_RECENT_TOKENS = 20_000
#: Tool results are serialized truncated to this many chars (specs/08 §3.5).
MAX_SERIALIZED_TOOL_CHARS = 2_000
#: Summary output cap: 80% of the compaction reserve.
SUMMARY_MAX_TOKENS_FRACTION = 0.8
#: Model pointer for summary requests (auxiliary — auto-falls back to main).
COMPACT_MODEL_POINTER = "compact"

SUMMARIZATION_PROMPT = """Summarize the conversation in <conversation>...</conversation>. This summary replaces that conversation in the next model call, so it must be self-contained: someone reading only the summary must know what was requested, what was done, and what remains.

Structure the summary in exactly these sections:

# Goal
# Constraints & Preferences
# Progress
## Done
## In Progress
## Blocked
# Key Decisions
# Next Steps
# Critical Context

Critical Context holds exact strings that must survive compaction: paths, commands, URLs, names, versions, error messages. Progress tracks what changed relative to the goal. Keep the summary dense; add nothing outside the sections.

<conversation>
{conversation}
</conversation>"""

UPDATE_SUMMARIZATION_PROMPT = """Below is an existing summary followed by new conversation. Update the summary: keep what is still true, revise what changed, mark new progress, add new facts. Follow the same section structure. Do not rewrite from scratch.

<existing-summary>
{previous_summary}
</existing-summary>
<conversation>
{conversation}
</conversation>"""


@dataclass
class CutPoint:
    """Where the conversation splits.

    ``index``: messages[:index] is compressed, messages[index:] is retained.
    ``turn_prefix``: for a split turn (cut on an assistant message), the
    turn's lead-in that got compressed — summarized separately so the
    retained assistant reply still has its question.
    """

    index: int
    turn_prefix: list[SessionMessage] | None = None


def _is_tool_result_carrier(msg: SessionMessage) -> bool:
    return (
        msg.role == "user"
        and isinstance(msg.content, list)
        and len(msg.content) > 0
        and all(b.type == "tool_result" for b in msg.content)
    )


def _is_legal_cut(msg: SessionMessage) -> bool:
    if msg.role == "assistant":
        return True
    # user messages that only carry tool results never cut (turn pairing)
    return not _is_tool_result_carrier(msg)


def _turn_start(messages: list[SessionMessage], index: int) -> int:
    """Index of the turn lead-in for the split-turn case: the nearest real
    user input before *index* (tool_result carriers and summaries excluded —
    a summary restarts the segment, it does not start a turn)."""
    for i in range(index - 1, -1, -1):
        msg = messages[i]
        if msg.role == "user" and not _is_tool_result_carrier(msg) and not msg.is_compaction_summary:
            return i
    return 0


def find_cut_point(
    messages: list[SessionMessage], keep_recent: int = DEFAULT_KEEP_RECENT_TOKENS
) -> CutPoint | None:
    """Find the latest cut point that leaves ≈keep_recent tokens intact.

    Returns None when the whole conversation fits in the window (nothing to
    compress) or no legal cut point exists (degenerate all-tool-result case).
    """
    if not messages:
        return None
    accumulated = 0
    cut_from: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        accumulated += estimate_message_tokens(messages[i])
        if accumulated >= keep_recent:
            cut_from = i
            break
    if cut_from is None:
        return None
    for i in range(cut_from, -1, -1):
        msg = messages[i]
        if not _is_legal_cut(msg):
            continue
        if i == 0:
            # the whole window is one message: compressing an empty span
            # would burn a summary call for nothing
            return None
        if msg.role == "user":
            return CutPoint(i)  # whole turn preserved (it starts at this user)
        return CutPoint(i, turn_prefix=messages[_turn_start(messages, i) : i])
    return None


# ---- serialization ----

def _truncate(text: str, limit: int = MAX_SERIALIZED_TOOL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (truncated, {len(text)} chars)"


def _serialize_block(block: ContentBlock) -> str:
    if block.type == "text":
        return block.text or ""
    if block.type == "thinking":
        return f"<thinking>{block.text or ''}</thinking>"
    if block.type == "tool_use":
        try:
            args = json.dumps(block.input, ensure_ascii=False)
        except TypeError:
            args = str(block.input)
        return f'<tool_call name="{block.name}" id="{block.id}">{args}</tool_call>'
    if block.type == "tool_result":
        flag = " error" if block.is_error else ""
        return f"<tool_result{flag}>{_truncate(str(block.content or ''))}</tool_result>"
    return ""


def _serialize_message(msg: SessionMessage) -> str:
    if isinstance(msg.content, str):
        return f"<user>{msg.content}</user>" if msg.role == "user" else f"<assistant>{msg.content}</assistant>"
    inner = "\n".join(_serialize_block(b) for b in msg.content)
    if msg.role == "user":
        return inner  # tool_result blocks already carry their own tags
    return f"<assistant>\n{inner}\n</assistant>"


def serialize_conversation(messages: list[SessionMessage]) -> str:
    """Lossy text projection of the conversation for the summary request."""
    body = "\n".join(_serialize_message(m) for m in messages)
    return f"<conversation>\n{body}\n</conversation>"


# ---- summary generation ----

def _find_previous_summary(messages: list[SessionMessage]) -> str | None:
    """The last existing summary in the compressed span (UPDATE iteration)."""
    for msg in reversed(messages):
        if msg.is_compaction_summary and isinstance(msg.content, str):
            return msg.content
    return None


def _summary_prompt(conversation: str, previous_summary: str | None) -> str:
    if previous_summary:
        return UPDATE_SUMMARIZATION_PROMPT.format(
            previous_summary=previous_summary, conversation=conversation
        )
    return SUMMARIZATION_PROMPT.format(conversation=conversation)


async def _request_summary(
    client: LLMClient, prompt: str, max_tokens: int
) -> str:
    response = await client.complete(
        LLMRequest(
            messages=[Message(role="user", content=prompt)],
            max_tokens=max_tokens,
        ),
        model=COMPACT_MODEL_POINTER,
    )
    if response.is_error or not response.text.strip():
        raise LLMError(
            response.error_message or "summary request returned no text",
            status_code=None,
        )
    return response.text.strip()


async def generate_summary(
    client: LLMClient,
    messages: list[SessionMessage],
    *,
    cut: CutPoint | None = None,
    max_tokens: int | None = None,
    previous_summary: str | None = None,
) -> str:
    """Summarize the compressed span (messages[:cut.index]).

    Split turns get two summary requests (history + the turn's lead-in),
    concatenated — the lead-in summary lands right before the retained
    assistant reply it answers.
    """
    cut = cut or find_cut_point(messages)
    if cut is None:
        return ""
    max_tokens = max_tokens or int(DEFAULT_RESERVE_TOKENS * SUMMARY_MAX_TOKENS_FRACTION)
    compressed = messages[: cut.index]
    prev = previous_summary if previous_summary is not None else _find_previous_summary(compressed)
    summary = await _request_summary(
        client, _summary_prompt(serialize_conversation(compressed), prev), max_tokens
    )
    if cut.turn_prefix:
        prefix = await _request_summary(
            client,
            _summary_prompt(serialize_conversation(cut.turn_prefix), None),
            max_tokens,
        )
        summary = f"{summary}\n\n{prefix}"
    return summary


def summary_message(text: str) -> SessionMessage:
    """The durable artifact of a compaction: a user message marked as summary."""
    return user_message(text, is_compaction_summary=True)
