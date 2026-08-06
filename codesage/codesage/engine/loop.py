"""AgentLoop: the main agent runtime (design note #1/#2/#4).

Kode implements the loop as a recursive async generator; Python's recursion
limit (1000) makes that fail after ~a thousand turns — R1. This loop uses an
explicit `while` over a growable message list: same transparent message
stream, zero stack growth (verified by the >2000-turn pressure test).

The loop yields SessionMessages; the final yield of each turn is the model's
response, and the loop terminates on: final answer, max_turns, max_budget,
or abort (three checkpoints: loop top, after LLM call, tool batch).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("codesage.engine")

from ..ai import ContentBlock, LLMClient, LLMError, LLMRequest, Message, StreamEvent
from ..ai.retry import is_ptl_error
from ..core import Session, SessionMessage, assistant_message, normalize_for_api, user_message
from ..permissions import PermissionDecision, PermissionEngine, PermissionMode
from ..permissions.store import load_permission_rules
from ..tools import ToolError, ToolRegistry, ToolResult, ToolUseContext
from .compaction import (
    CompactionConfig,
    CutPoint,
    FileOps,
    clean_old_tool_results,
    extract_file_ops,
    find_cut_point,
    find_previous_summary,
    generate_summary,
    recovery_reminder_text,
    summary_message,
)
from .context import ContextBundle
from .tokens import estimate_context_tokens, should_compact
from .tool_queue import ScheduledTool, ToolUseQueue

#: Synthesized messages (is_meta — filtered by normalize_for_api).
INTERRUPT_TEXT = "(interrupted by user)"
MAX_TURNS_TEXT = "Stopped: maximum turn count reached."
MAX_BUDGET_TEXT = "Stopped: maximum budget reached."
#: system-reminder section cap (todo.md acceptance: system-reminder 上限 10).
MAX_REMINDER_SECTIONS = 10

#: system-reminder wrapper (Claude Code prependUserContext shape).
REMINDER_HEADER = (
    "<system-reminder>\n"
    "As you answer the user's questions, you can use the following context:\n"
)
REMINDER_FOOTER = "\n\nIMPORTANT: this context may or may not be relevant to your tasks.\n</system-reminder>"
#: Injected when the model replies with only internal reasoning (bounded retries).
THINKING_ONLY_RECOVERY = "你只输出了内部思考,请直接输出回复或调用工具"
THINKING_ONLY_GIVE_UP = "Model returned internal reasoning only; giving up after 3 attempts"
THINKING_ONLY_MAX_RETRIES = 3


def _is_thinking_only(message: SessionMessage) -> bool:
    """True when the assistant reply contains only thinking blocks (no text/tool_use)."""
    content = message.content
    return (
        not message.is_error
        and isinstance(content, list)
        and len(content) > 0
        and all(b.type == "thinking" for b in content)
    )


class AgentLoop:
    def __init__(
        self,
        *,
        client: LLMClient,
        tools: ToolRegistry,
        permissions: PermissionEngine | None = None,
        request_permission: Callable[[PermissionDecision, Any, dict], Awaitable[bool]] | None = None,
        system_prompt: str = "",
        context_bundle: ContextBundle | None = None,  # session context → system-reminder (S4)
        compaction: CompactionConfig | None = None,  # PI-05 auto-compact (S6; None disables)
        model: str = "main",
        mode: str | PermissionMode = PermissionMode.DEFAULT,
        max_turns: int = 100,
        max_budget_usd: float | None = None,
        cwd: Path | None = None,
        session: Session | None = None,
        settings: Any = None,  # phase-01 Settings for permission rules
        session_permissions: dict | None = None,  # "this session only" rules (CC-07)
        history: list["SessionMessage"] | None = None,  # prior turns as context (--continue)
        on_stream: Callable[["StreamEvent"], None] | None = None,  # live text deltas for UI
        steer_queue: "asyncio.Queue[str] | None" = None,  # mid-run user inputs (PI-06)
        on_tool_event: Callable[[str, str, dict], None] | None = None,  # start/update/end (PI-01)
        finalize: Callable[["ScheduledTool", "ToolResult"], "Awaitable[ToolResult]"] | None = None,  # result rewrite hook (PI-02)
    ):
        self.client = client
        self.tools = tools
        self.permissions = permissions or PermissionEngine()
        self.request_permission = request_permission
        self.system_prompt = system_prompt
        self.context_bundle = context_bundle
        self.compaction = compaction
        self._last_compact_turn = -1  # debounce: one compaction per turn
        self._compact_failures = 0  # consecutive failures → breaker
        # §3.6: one-shot reminder with recently modified files, injected into
        # the request right after a compaction (never persisted)
        self._recovery_reminder: str | None = None
        # §3.7: last time the request view cleared old tool results
        self._last_result_clean = time.time()
        # §3.8: reactive compaction — one PTL recovery per loop run
        self._ptl_retried = False
        # §3.9: previous response's cache_read_tokens (break detection)
        self._last_cache_read = 0
        self.model = model
        self.mode = mode
        self.max_turns = max_turns if isinstance(max_turns, int) and max_turns > 0 else 100
        self.max_budget_usd = max_budget_usd
        self.cwd = cwd or Path.cwd()
        self.session = session
        self.settings = settings
        self.session_permissions = session_permissions
        self.history = history or []
        self.on_stream = on_stream
        self.steer_queue = steer_queue
        self.on_tool_event = on_tool_event
        self.finalize = finalize
        #: Termination reason of the last run(): "completed" | "max_turns" |
        #: "max_budget" | "interrupted" | "error" | "thinking_only_exhausted" |
        #: "tool_terminated" (CC-10 / PI-04)
        self.last_stop_reason: str | None = None
        self.abort = asyncio.Event()
        #: One ToolUseContext per loop: carries read-freshness state and the
        #: abort channel across tool calls (phase 03 read-first guard).
        self._tool_ctx: ToolUseContext | None = None

    # ---- public entry ----

    async def run(self, user_input: str | list[ContentBlock]) -> AsyncIterator[SessionMessage]:
        """Run the loop from a user input; yields the conversation messages."""
        first = user_message(user_input)
        yield first
        await self._persist(first)

        # resumed turns start from the prior history; the session file keeps
        # growing so --continue chains naturally across runs
        messages: list[SessionMessage] = [*self.history, first]
        turn = 0
        thinking_retries = 0
        try:
            while True:
                if turn >= self.max_turns:
                    yield await self._stop("max_turns", MAX_TURNS_TEXT)
                    return
                if self.max_budget_usd is not None and self.client.total_cost[0] >= self.max_budget_usd:
                    yield await self._stop("max_budget", MAX_BUDGET_TEXT)
                    return
                if self.abort.is_set():
                    yield await self._stop("interrupted", INTERRUPT_TEXT, meta=True)
                    return
                turn += 1

                # PI-05 checkpoint: auto-compaction, after abort/budget/turns
                # (specs/08 §3.5). Pipeline order follows CC query.ts:379-468 —
                # the cheap local level (microcompact: old tool results →
                # placeholder) runs BEFORE the LLM level, and the threshold
                # check estimates the CLEANED view: a cleanup that frees
                # enough tokens makes the summary call unnecessary (auto-
                # compact is the last resort). The summary request does not
                # count as a turn.
                if self.compaction is not None and self.compaction.enabled and turn != self._last_compact_turn:
                    # Estimative cleanup only — deliberately do NOT touch
                    # _last_result_clean: that gate is shared with _ask_model's
                    # request-view projection, and consuming the stale trigger
                    # here would leave the actual request un-cleaned (review:
                    # the checkpoint must not eat the other site's gate).
                    cleaned, _ = clean_old_tool_results(
                        messages, now=time.time(), last_clean=self._last_result_clean
                    )
                    # clean_old_tool_results returns `messages` itself when
                    # nothing was cleared — safe to estimate unconditionally
                    estimate = estimate_context_tokens(cleaned)
                    if should_compact(estimate.tokens, self.compaction.window, self.compaction.reserve):
                        self._last_compact_turn = turn  # debounce: no second pass this turn
                        compacted = await self._compact(messages)  # RAW span: summary keeps full detail
                        if compacted is not None:
                            summary_msg, cut = compacted
                            yield summary_msg
                            messages = [summary_msg, *messages[cut.index :]]

                # PI-06: drain mid-run steer inputs into the conversation
                # (they become user messages for the next LLM call)
                if self.steer_queue is not None:
                    steers: list[str] = []
                    while True:
                        try:
                            steers.append(self.steer_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    for text in steers:
                        steer_msg = user_message(text)
                        yield steer_msg
                        await self._persist(steer_msg)
                        messages.append(steer_msg)

                # LLM call (checkpoint 1: abort before and after)
                try:
                    assistant = await self._ask_model(messages)
                except LLMError as exc:
                    if is_ptl_error(exc) and not self._ptl_retried and self.compaction is not None:
                        # §3.8 reactive compaction: PTL is the one state a
                        # threshold-triggered compact can't prevent (huge
                        # tool result, compaction disabled). Force a compact
                        # (skip should_compact) and retry once; a failing
                        # compact falls through to the outer error path and
                        # counts into the breaker (no PTL-compact-PTL loop).
                        self._ptl_retried = True
                        compacted = await self._compact(messages)
                        if compacted is not None:
                            summary_msg, cut = compacted
                            yield summary_msg
                            messages = [summary_msg, *messages[cut.index :]]
                            continue
                    raise
                if assistant is None:
                    yield await self._stop("interrupted", INTERRUPT_TEXT, meta=True)
                    return
                yield assistant
                await self._persist(assistant)
                messages.append(assistant)

                tool_uses = [b for b in assistant.content if b.type == "tool_use"] if isinstance(assistant.content, list) else []
                if not tool_uses:
                    if _is_thinking_only(assistant):
                        thinking_retries += 1
                        if thinking_retries >= THINKING_ONLY_MAX_RETRIES:
                            yield await self._stop("thinking_only_exhausted", THINKING_ONLY_GIVE_UP, meta=True)
                            return
                        # retry with a recovery nudge (counted separately, not as a turn)
                        recovery = user_message(THINKING_ONLY_RECOVERY)
                        yield recovery
                        await self._persist(recovery)
                        messages.append(recovery)
                        turn -= 1
                        continue
                    self.last_stop_reason = "completed"  # final answer — terminate
                    return

                # execute tools (checkpoint 2: abort before the batch)
                if self.abort.is_set():
                    yield await self._stop("interrupted", INTERRUPT_TEXT, meta=True)
                    return
                scheduled = await self._execute_tools(tool_uses)
                # one tool_result user message per tool, in tool_use order
                # (normalize_for_api merges adjacent user messages — wire behavior unchanged)
                for item in scheduled:
                    tool_round = user_message(
                        [
                            ContentBlock(
                                type="tool_result",
                                tool_use_id=item.tool_use_id,
                                content=item.result.content if item.result else "(no result)",
                                is_error=item.result.is_error if item.result else True,
                            )
                        ]
                    )
                    yield tool_round
                    await self._persist(tool_round)
                    messages.append(tool_round)
                # PI-04: terminate semantics — the turn stops only when EVERY
                # tool in the batch asks to stop. Checked AFTER the tool results
                # were yielded so the model/user still see what the tools did.
                if scheduled and all(
                    item.result is not None and item.result.terminate for item in scheduled
                ):
                    yield await self._stop("tool_terminated", "Stopped: tools requested termination.")
                    return
        except LLMError as exc:
            # unrecoverable provider error surfaces as a message, not a crash
            self.last_stop_reason = "error"
            failed = assistant_message(
                f"(provider error: {exc})",
                is_error=True,
            )
            yield failed
            await self._persist(failed)

    # ---- internals ----

    async def _compact(self, messages: list[SessionMessage]) -> tuple[SessionMessage, CutPoint] | None:
        """Summarize the history span and persist the summary (append-only).

        Returns (summary_message, cut) on success, None when there is nothing
        to compress or the request failed. Two consecutive failures trip the
        breaker (specs/08 §3.5): the feature disables itself instead of
        burning a summary call every turn.
        """
        cut = find_cut_point(messages, keep_recent=self.compaction.keep_recent)
        if cut is None:
            return None  # not a success — the failure streak stays intact (breaker)
        compressed = messages[: cut.index]
        prev_summary = find_previous_summary(compressed)
        try:
            summary = await generate_summary(
                self.client, messages, cut=cut, previous_summary=prev_summary
            )
        except LLMError:
            self._compact_failures += 1
            if self._compact_failures >= 2:
                self.compaction.enabled = False  # breaker (specs/08 §3.5)
            return None
        self._compact_failures = 0
        # fileOps merge across rounds (§3.6): previous summary's lists + what
        # this compressed span touched; appended to the summary tail so the
        # info survives --continue replays
        ops: FileOps = FileOps.parse(prev_summary).merged_with(extract_file_ops(compressed))
        summary_msg = summary_message(ops.append_to(summary))
        self._recovery_reminder = recovery_reminder_text(ops, self.cwd)  # one-shot (next request)
        await self._persist(summary_msg)  # append-only: compaction appends one summary
        return summary_msg, cut

    async def _ask_model(self, messages: list[SessionMessage]) -> SessionMessage | None:
        # §3.7 projection: old tool results cleared in the request view only —
        # the session log stays append-only (specs/08 §3.7). Independent of
        # compaction: it is most useful when compaction is off (messages only grow).
        now = time.time()
        cleaned, did = clean_old_tool_results(
            messages, now=now, last_clean=self._last_result_clean
        )
        if did:
            self._last_result_clean = now
            messages = cleaned
        api_messages = normalize_for_api(messages)
        prefix: list[Message] = []
        if self.context_bundle is not None:
            # context rides as a hoisted system-reminder user message, never
            # in `system` — the system prefix stays byte-stable for server
            # prefix caching (specs/08 §3.4); the reminder is request-only,
            # it never enters the session log
            prefix.append(_render_reminder(self.context_bundle))
        if self._recovery_reminder is not None:
            # §3.6 restore: recently modified files, injected once right
            # after the compaction that produced them
            prefix.append(
                Message(
                    role="user",
                    content=f"{REMINDER_HEADER}{self._recovery_reminder}{REMINDER_FOOTER}",
                )
            )
            self._recovery_reminder = None
        if prefix:
            api_messages = [*prefix, *api_messages]
        request = LLMRequest(
            messages=api_messages,
            system=self.system_prompt,
            tools=self.tools.specs(),
        )
        stream = self.client.stream(request, model=self.model)
        if self.on_stream is not None:
            # tee: UI gets live deltas, the loop still gets the assembled result
            # (capture a local copy — the closure must not see the reassignment)
            inner = stream

            async def _tee():
                async for ev in inner:
                    self.on_stream(ev)
                    yield ev

            stream = _tee()
        response = await LLMClient.collect(stream)
        if self.abort.is_set():
            return None
        # §3.9 cache-break detection (light): a drop from a high
        # cache_read_tokens to 0 means the server prefix cache was invalidated
        # (TTL expiry or server eviction). Diagnostic only — no action.
        if response.usage is not None:
            cache_read = response.usage.cache_read_tokens
            if cache_read == 0 and self._last_cache_read > 0:
                logger.warning(
                    "cache break: cache_read_tokens dropped %d -> 0 (TTL expiry or server eviction)",
                    self._last_cache_read,
                )
            self._last_cache_read = cache_read
        return assistant_message(
            response.content,
            usage=response.usage,
            model=response.model,
            is_error=response.is_error,
            error_message=response.error_message,
        )

    async def _execute_tools(self, tool_uses: list[ContentBlock]) -> list[ScheduledTool]:
        scheduled: list[ScheduledTool] = []
        if self._tool_ctx is None:
            self._tool_ctx = ToolUseContext(cwd=self.cwd, abort_event=self.abort)
        ctx = self._tool_ctx
        for block in tool_uses:
            tool = self.tools.get(block.name or "")
            if tool is None:
                scheduled.append(
                    ScheduledTool(
                        tool_use_id=block.id or "",
                        tool=_MissingTool(block.name or ""),
                        input={},
                        context=ctx,
                        result=ToolResult(f"Unknown tool: {block.name}", is_error=True),
                        status="completed",
                    )
                )
                continue
            tool_input = block.input or {}
            try:
                tool.validate_input(tool_input)
            except ToolError as exc:
                # invalid input: report without executing; marked done so the
                # queue skips it and never poisons its siblings
                scheduled.append(
                    ScheduledTool(
                        tool_use_id=block.id or "",
                        tool=tool,
                        input=tool_input,
                        context=ctx,
                        status="completed",
                        result=ToolResult(str(exc), is_error=True),
                    )
                )
                continue
            scheduled.append(
                ScheduledTool(
                    tool_use_id=block.id or "",
                    tool=tool,
                    input=tool_input,
                    context=ctx,
                )
            )
        queue = ToolUseQueue(
            scheduled,
            permission_check=self._permission_check,
            on_tool_event=self.on_tool_event,
            finalize=self.finalize,
        )
        return await queue.run()

    async def _permission_check(self, item: ScheduledTool) -> ToolResult | None:
        """Return a denial ToolResult, or None to allow execution."""
        decision = self.permissions.evaluate_tool_use(
            tool_name=item.tool.name,
            tool_input=item.input,
            tool=item.tool,
            mode=self.mode,
            cwd=self.cwd,
            permissions=load_permission_rules(self.settings) if self.settings is not None else None,
            session_permissions=self.session_permissions,
        )
        if decision.allowed:
            return None
        if decision.mode == "ask" and self.request_permission is not None:
            if await self.request_permission(decision, item.tool, item.input):
                return None
        return ToolResult(f"Permission denied: {decision.reason}", is_error=True)

    async def _stop(self, reason: str, text: str, *, meta: bool = False) -> SessionMessage:
        """Termination point: record the stop reason, then emit the final message."""
        self.last_stop_reason = reason
        return await self._finish(text, meta=meta)

    async def _finish(self, text: str, *, meta: bool = False) -> SessionMessage:
        message = assistant_message(text, is_meta=meta)
        await self._persist(message)
        return message

    async def _persist(self, message: SessionMessage) -> None:
        if self.session is not None:
            self.session.append(message)


def _render_reminder(bundle: ContextBundle) -> Message:
    """Render the context bundle as one system-reminder user message.

    A plain ai.Message: it is already hoisted at the front, so it does not
    need the normalize_for_api reminder pass (and must not be a
    SessionMessage — LLMRequest.messages is list[Message]). Session history
    never carries is_reminder messages, so normalize_for_api sees none.

    Section budget: date/git always stay; AGENTS.md sections (far → near in
    the bundle) keep the NEAREST ones within the cap — the recency-priority
    files must not be the ones dropped at the 10-section limit.
    """
    fixed = [s for s in bundle.sections if s[0] != "agentsMd"]
    agents = [s for s in bundle.sections if s[0] == "agentsMd"]
    if len(fixed) >= MAX_REMINDER_SECTIONS:
        fixed = fixed[:MAX_REMINDER_SECTIONS]
        agents = []
    else:
        agents = agents[-(MAX_REMINDER_SECTIONS - len(fixed)) :]
    parts = [REMINDER_HEADER]
    for title, text in fixed + agents:
        # trailing newline: the next "# title" must not glue onto this text
        parts.append(f"# {title}\n{text}\n")
    parts.append(REMINDER_FOOTER)
    return Message(role="user", content="".join(parts))


class _MissingTool:
    """Placeholder for unknown tool names; reports itself as failed."""

    is_concurrency_safe = True

    def __init__(self, name: str):
        self.name = name

    def needs_permissions(self, input: dict) -> bool:
        return False
