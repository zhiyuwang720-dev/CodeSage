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
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..ai import ContentBlock, LLMClient, LLMError, LLMRequest, Message
from ..ai.retry import is_ptl_error
from ..core import SessionMessage, user_message
from .tokens import DEFAULT_CONTEXT_WINDOW, DEFAULT_RESERVE_TOKENS, estimate_message_tokens

#: Recent tokens kept intact at the end of the conversation (pi keepRecentTokens).
DEFAULT_KEEP_RECENT_TOKENS = 20_000
#: Tool results are serialized truncated to this many chars (specs/08 §3.5).
MAX_SERIALIZED_TOOL_CHARS = 2_000
#: Summary output cap: 80% of the compaction reserve.
SUMMARY_MAX_TOKENS_FRACTION = 0.8
#: Model pointer for summary requests (auxiliary — auto-falls back to main).
COMPACT_MODEL_POINTER = "compact"

# ---- §3.6 fileOps restore ----
#: File ops tracked across compaction rounds (pi fileOps).
FILE_OPS_TOOLS = {"Read": "read", "Write": "modified", "Edit": "modified"}
#: Most recent modified files re-injected after compaction (CC: 5 files).
RECOVERY_MAX_FILES = 5
#: Per-file content cap ≈5K tokens (4 chars/token).
RECOVERY_MAX_CHARS_PER_FILE = 20_000

# ---- §3.7 old tool result cleanup ----
#: Cleanup fires when the message list exceeds this (CC microcompact count path).
MAX_RESULTS_BEFORE_CLEAN = 60
#: Newest results kept intact.
MAX_RESULTS_KEPT = 20
#: ...or when the last cleanup is older than this.
CLEAN_INTERVAL_SECONDS = 30 * 60
#: Replacement text for cleared results (specs/08 §3.7).
OLD_RESULT_PLACEHOLDER = "[Old tool result content cleared — see session log]"
#: Whitelist: only these tools' results may be cleared (specs/08 §3.7).
CLEANABLE_TOOLS = frozenset({"Read", "Bash", "Grep", "Glob"})

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
class CompactionConfig:
    """Auto-compaction knobs (specs/08 §3.5). None on the loop disables it."""

    enabled: bool = True
    window: int = DEFAULT_CONTEXT_WINDOW
    reserve: int = DEFAULT_RESERVE_TOKENS
    keep_recent: int = DEFAULT_KEEP_RECENT_TOKENS


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

def find_previous_summary(messages: list[SessionMessage]) -> str | None:
    """The last existing summary in the compressed span (UPDATE iteration)."""
    for msg in reversed(messages):
        if msg.is_compaction_summary and isinstance(msg.content, str):
            return msg.content
    return None


def _summary_prompt(
    conversation: str, previous_summary: str | None, extra_instructions: str | None = None
) -> str:
    if previous_summary:
        prompt = UPDATE_SUMMARIZATION_PROMPT.format(
            previous_summary=previous_summary, conversation=conversation
        )
    else:
        prompt = SUMMARIZATION_PROMPT.format(conversation=conversation)
    if extra_instructions:
        # 阶段 09 §7.4:PreCompact 钩子 custom instructions —— 追加在 conversation
        # 标签之后(指令在会话外,不混入会话内容);请求视图内一次性构造,不落会话
        prompt = f"{prompt}\n\n# Custom Instructions\n{extra_instructions}"
    return prompt


def _drop_oldest_turn(messages: list[SessionMessage]) -> list[SessionMessage] | None:
    """Trim the oldest full turn for a PTL retry (specs/08 §3.8): everything
    up to the second real user input is dropped. None when fewer than two
    user turns exist (nothing meaningful to drop — propagate instead).

    Compaction summaries are not turns (same rule as _turn_start): in a
    multi-compaction session the span starts with a summary, and counting
    it as a user input would make the "retry" drop only the summary —
    the input barely shrinks and the second PTL fails (review R1)."""
    users = [
        i
        for i, msg in enumerate(messages)
        if msg.role == "user" and not _is_tool_result_carrier(msg) and not msg.is_compaction_summary
    ]
    if len(users) < 2:
        return None
    return messages[users[1] :]


async def _request_summary(
    client: LLMClient,
    prompt: str,
    max_tokens: int,
    *,
    retry_messages: list[SessionMessage] | None = None,
    previous_summary: str | None = None,
    extra_instructions: str | None = None,  # 阶段 09 §7.4:PreCompact custom instructions
) -> str:
    """One summary request; on PTL, re-serialize with the oldest turn dropped
    and retry exactly once (the request itself can exceed the window when the
    conversation is already enormous)."""
    try:
        response = await client.complete(
            LLMRequest(
                messages=[Message(role="user", content=prompt)],
                max_tokens=max_tokens,
            ),
            model=COMPACT_MODEL_POINTER,
        )
    except LLMError as exc:
        if not is_ptl_error(exc) or retry_messages is None:
            raise
        trimmed = _drop_oldest_turn(retry_messages)
        if trimmed is None:
            raise
        retry_prompt = _summary_prompt(
            serialize_conversation(trimmed), previous_summary, extra_instructions=extra_instructions
        )
        response = await client.complete(
            LLMRequest(
                messages=[Message(role="user", content=retry_prompt)],
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
    extra_instructions: str | None = None,  # 阶段 09 §7.4:PreCompact custom instructions
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
    prev = previous_summary if previous_summary is not None else find_previous_summary(compressed)
    summary = await _request_summary(
        client,
        _summary_prompt(
            serialize_conversation(compressed), prev, extra_instructions=extra_instructions
        ),
        max_tokens,
        retry_messages=compressed,
        previous_summary=prev,
        extra_instructions=extra_instructions,
    )
    if cut.turn_prefix:
        prefix = await _request_summary(
            client,
            _summary_prompt(
                serialize_conversation(cut.turn_prefix),
                None,
                extra_instructions=extra_instructions,
            ),
            max_tokens,
            retry_messages=cut.turn_prefix,
            previous_summary=None,
            extra_instructions=extra_instructions,
        )
        summary = f"{summary}\n\n{prefix}"
    return summary


def summary_message(text: str) -> SessionMessage:
    """The durable artifact of a compaction: a user message marked as summary."""
    return user_message(text, is_compaction_summary=True)


# ---- §3.6 fileOps (pi fileOps: which files the compressed span touched) ----

_TAG_RE = re.compile(r"<(read-files|modified-files)>(.*?)</\1>", re.S)


@dataclass
class FileOps:
    """File operations extracted from a compressed span, merged across rounds.

    Persisted as <read-files>/<modified-files> sections at the summary tail so
    the info survives --continue replays; parse() recovers it for the next
    round's merge (UPDATE iteration).
    """

    read: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str | None) -> "FileOps":
        read: list[str] = []
        modified: list[str] = []
        for tag, body in _TAG_RE.findall(text or ""):
            items = [line.strip() for line in body.splitlines() if line.strip()]
            if tag == "read-files":
                read = items
            else:
                modified = items
        return cls(read=read, modified=modified)

    def merged_with(self, newer: "FileOps") -> "FileOps":
        """Newly extracted paths first; dedupe preserving order."""

        def _merge(new: list[str], old: list[str]) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for path in [*new, *old]:
                if path not in seen:
                    seen.add(path)
                    out.append(path)
            return out

        return FileOps(_merge(newer.read, self.read), _merge(newer.modified, self.modified))

    def append_to(self, text: str) -> str:
        if not self.read and not self.modified:
            return text
        parts = [text]
        if self.read:
            parts.append("<read-files>\n" + "\n".join(self.read) + "\n</read-files>")
        if self.modified:
            parts.append("<modified-files>\n" + "\n".join(self.modified) + "\n</modified-files>")
        return "\n".join(parts)


def extract_file_ops(messages: list[SessionMessage]) -> FileOps:
    """Scan tool_use blocks in *messages* for Read/Write/Edit file paths.

    Newest first (reverse message order) so recovery injects the most recent
    edits. Only calls that actually EXECUTED successfully count: a denied or
    failed tool (is_error result) must not record its path — recovery would
    otherwise read back files the permission gates refused to touch. A
    tool_use with no paired result (e.g. aborted mid-batch) is also skipped.
    """
    executed_ids = {
        block.tool_use_id
        for msg in messages
        if isinstance(msg.content, list)
        for block in msg.content
        if block.type == "tool_result" and not block.is_error and block.tool_use_id
    }
    read: list[str] = []
    modified: list[str] = []
    for msg in reversed(messages):
        content = msg.content
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if block.type != "tool_use" or block.id not in executed_ids:
                continue
            kind = FILE_OPS_TOOLS.get(block.name or "")
            path = (block.input or {}).get("file_path") if isinstance(block.input, dict) else None
            if kind and isinstance(path, str) and path:
                (read if kind == "read" else modified).append(path)
    return FileOps(read=read, modified=modified)


def recovery_reminder_text(ops: FileOps, cwd: Path) -> str | None:
    """Restore context after compaction: the most recent modified files,
    content attached (specs/08 §3.6). Unreadable/oversized files are skipped.
    """
    if not ops.modified:
        return None
    parts = ["Recently modified files:"]
    for path in ops.modified[:RECOVERY_MAX_FILES]:
        try:
            content = (cwd / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(content) > RECOVERY_MAX_CHARS_PER_FILE:
            content = content[:RECOVERY_MAX_CHARS_PER_FILE] + "\n...(truncated)"
        parts.append(f"# {path}\n{content}")
    if len(parts) == 1:
        return None  # no file was readable
    return "\n\n".join(parts)


# ---- §3.7 old tool result cleanup (request-view projection) ----

def clean_old_tool_results(
    messages: list[SessionMessage],
    *,
    max_results: int = MAX_RESULTS_BEFORE_CLEAN,
    keep_recent: int = MAX_RESULTS_KEPT,
    now: float | None = None,
    last_clean: float | None = None,
) -> tuple[list[SessionMessage], bool]:
    """Projection: replace old whitelisted tool_result payloads with a
    placeholder in the request view. Never touches the persisted log.

    Fires when the message list exceeds *max_results*, or when the last
    cleanup is older than CLEAN_INTERVAL_SECONDS (and anything is clearable).
    The newest *keep_recent* results always stay intact.
    """
    tool_by_id: dict[str, str] = {}
    results: list[tuple[int, str]] = []  # (message index, tool name)
    for idx, msg in enumerate(messages):
        content = msg.content
        if not isinstance(content, list):
            continue
        for block in content:
            if block.type == "tool_use" and block.id:
                tool_by_id[block.id] = block.name or ""
            elif block.type == "tool_result" and block.tool_use_id:
                results.append((idx, tool_by_id.get(block.tool_use_id, "")))
    if not results:
        return messages, False
    stale = now is not None and last_clean is not None and now - last_clean > CLEAN_INTERVAL_SECONDS
    if len(messages) <= max_results and not stale:
        return messages, False
    # keep_recent=0 would slice results[:-0] == [] — clear everything instead
    older = results if keep_recent <= 0 else results[:-keep_recent]
    clearable = [idx for idx, name in older if name in CLEANABLE_TOOLS]
    if not clearable:
        return messages, False
    out = list(messages)
    for idx in clearable:
        blocks = [
            block.model_copy(update={"content": OLD_RESULT_PLACEHOLDER})
            if block.type == "tool_result" and block.content
            else block
            for block in out[idx].content
        ]
        out[idx] = replace(out[idx], content=blocks)
    return out, True
