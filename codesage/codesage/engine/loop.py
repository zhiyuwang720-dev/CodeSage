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
from ..ai.retry import is_ptl_error, is_ptl_text
from ..hooks import HookDispatchResult, HookInput, HookManager  # 阶段 09:事件钩子(HookManager 协议/实现)
from ..core import Session, SessionMessage, assistant_message, normalize_for_api, user_message
from ..permissions import PermissionDecision, PermissionEngine, PermissionMode, ToolAuditEvent
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
#: Prefetch callback bail-out: a hung memory search must not block the loop.
PREFETCH_TIMEOUT_S = 60

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
#: Stop feedback 注入上限(M1,09 补,对齐 CC MAX_STOP_HOOK_ATTEMPTS=5):
#: 达限后不再注入 feedback、按普通 completed/tool_terminated 停止,不报错——
#: 防「永远 exit 2」的钩子拖出无限循环(§6.4)。
MAX_STOP_HOOK_ATTEMPTS = 5


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
        prefetch: Callable[[list["SessionMessage"]], Awaitable[list[tuple[str, str]]]] | None = None,  # §3.10 memory prefetch (phase 17 fills in)
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
        hooks: HookManager | None = None,  # 阶段 09:事件钩子管理器(None = 禁用;S10 装配传入)
    ):
        self.client = client
        self.tools = tools
        self.permissions = permissions or PermissionEngine()
        self.request_permission = request_permission
        self.system_prompt = system_prompt
        self.context_bundle = context_bundle
        self.compaction = compaction
        self.prefetch = prefetch
        self._prefetch_task: asyncio.Task | None = None  # §3.10 in-flight prefetch
        self._last_compact_turn = -1  # debounce: one compaction per turn
        self._compact_failures = 0  # consecutive failures → breaker
        # §3.6: one-shot reminder with recently modified files, injected into
        # the request right after a compaction (never persisted)
        self._recovery_reminder: str | None = None
        # §3.7: last time the request view cleared old tool results
        self._last_result_clean = time.time()
        # §3.8: reactive compaction — one PTL recovery per loop run
        self._ptl_retried = False
        #: active message list exposed for the CLI status bar's ctx meter
        self._active_messages: list["SessionMessage"] | None = None
        # §3.9: previous response's cache_read_tokens (break detection)
        self._last_cache_read = 0
        self.model = model
        self.mode = mode
        if max_turns is not None and (not isinstance(max_turns, int) or max_turns <= 0):
            # a mistyped config silently becoming 100 turns would run the
            # model 100 rounds for nothing (review P3-3)
            raise ValueError(f"max_turns must be a positive int or None, got {max_turns!r}")
        self.max_turns = max_turns if max_turns is not None else 100
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
        self.hooks = hooks  # 阶段 09:事件钩子管理器(None = 钩子层零路径)
        #: SessionStart 门闩(§6.2):首个 run() 置位,AgentLoop 生命周期内只触发一次
        self._session_started = False
        #: updatedSystemReminder/additionalContext 累积(§7.1/§7.2):下一次请求的
        #: 一次性 prefix,注入后清除(与 _recovery_reminder 同款模式)
        self._hook_reminder: str | None = None
        #: Stop feedback 注入计数(M1,§6.4):run() 生命周期,达 MAX_STOP_HOOK_ATTEMPTS
        #: 后不再注入(与 turn 同生命周期,run() 入口重置)
        self._stop_feedback_count = 0
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
        # per-run state: a reused loop instance must start fresh (P3-8)
        self._ptl_retried = False
        self._last_cache_read = 0
        self._active_messages = None
        self._stop_feedback_count = 0  # M1:feedback 计数与 turn 同生命周期(§6.4)
        # SessionStart 钩子(§6.2):run() 入口,门闩一次 —— 首个 run() 派发后置位,
        # 生命周期内不再触发;非阻塞(§4.6 表:非 PreToolUse 事件 fail-open)
        if not self._session_started:
            self._session_started = True
            await self._dispatch_session_start()
        # UserPromptSubmit 钩子(§6.2):首条输入,user_message() 之前;exit 2 →
        # 阻止提交(输入擦除),钩子 stderr 作为终结消息
        user_input, blocked = await self._dispatch_user_prompt(user_input)
        if blocked is not None:
            yield await self._stop("hook_blocked", blocked, meta=True)
            return
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
                # CLI status bar's ctx meter reads this (compaction visibly
                # drops it); updated again after a compaction replaces it
                self._active_messages = messages
                if turn >= self.max_turns:
                    yield await self._stop("max_turns", MAX_TURNS_TEXT, meta=True)
                    return
                if self.max_budget_usd is not None and self.client.total_cost[0] >= self.max_budget_usd:
                    yield await self._stop("max_budget", MAX_BUDGET_TEXT, meta=True)
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
                            self._active_messages = messages  # meter reflects the compaction

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
                        # UserPromptSubmit 钩子(§6.2):steer 输入,user_message() 之前;
                        # blocked → 静默丢弃 + 日志,只影响该条输入,不中断运行
                        submitted, blocked = await self._dispatch_user_prompt(text)
                        if blocked is not None:
                            logger.warning(
                                "UserPromptSubmit hook blocked a steer input (§6.2): dropped"
                            )
                            continue
                        steer_msg = user_message(submitted)
                        yield steer_msg
                        await self._persist(steer_msg)
                        messages.append(steer_msg)

                # LLM call (checkpoint 1: abort before and after)
                try:
                    assistant = await self._ask_model(messages)
                except LLMError as exc:
                    if (
                        is_ptl_error(exc)
                        and not self._ptl_retried
                        and self.compaction is not None
                        and self.compaction.enabled  # breaker tripped: no more summary calls (P3-4)
                    ):
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
                            self._active_messages = messages  # meter reflects it now (P3-5)
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
                    # an is_error response (provider failure with no text) is
                    # NOT a completed turn — stop reason must say so (P3-7)
                    self.last_stop_reason = "error" if assistant.is_error else "completed"
                    if assistant.is_error:
                        return
                    # Stop 钩子(§6.4):completed 分支(门控表:error 不触发)。钩子可
                    # 拦下停止(continue:false)或注入 feedback 让模型再决策一轮
                    result = await self._dispatch_stop("completed", assistant)
                    if result is not None:
                        if result.stop:
                            # S5 m2:显式 continue:false 优先级 > exit 2 feedback;
                            # 钩子自产的停止不再触发 Stop 钩子(§2.2 防递归:直接 return)
                            yield await self._stop(
                                "hook", result.stop_reason or "Stopped: hook requested stop.", meta=True
                            )
                            return
                        if result.stop_feedback is not None and self._allow_stop_feedback():
                            # m3/§6.4 澄清:feedback 必须是普通 user_message —— is_meta
                            # 会被 normalize_for_api 过滤,模型不可见;注入后下一轮
                            # 计为一次 turn(n2)
                            feedback = user_message(f"Stop hook feedback:\n{result.stop_feedback}")
                            yield feedback
                            await self._persist(feedback)
                            messages.append(feedback)
                            continue
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
                    # Stop 钩子(§6.4):tool_terminated 分支(_stop 前);结果已在上面
                    # yield,钩子拦下后模型仍看得到工具做了什么
                    result = await self._dispatch_stop("tool_terminated", assistant)
                    if result is not None and result.stop:
                        yield await self._stop(
                            "hook", result.stop_reason or "Stopped: hook requested stop.", meta=True
                        )
                        return
                    if result is not None and result.stop_feedback is not None and self._allow_stop_feedback():
                        # m3/§6.4 澄清:feedback 必须普通 user_message(is_meta 被过滤,
                        # 模型不可见);下一轮计为一次 turn(n2)
                        feedback = user_message(f"Stop hook feedback:\n{result.stop_feedback}")
                        yield feedback
                        await self._persist(feedback)
                        messages.append(feedback)
                        continue
                    yield await self._stop("tool_terminated", "Stopped: tools requested termination.", meta=True)
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
        finally:
            # a prefetch still in flight must not dangle past the loop
            task = self._prefetch_task
            if task is not None and not task.done():
                task.cancel()

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
        # §3.10: a prefetch launched last turn that finished during the model
        # response / tool execution rides THIS request (CC attachment message:
        # injected after tool execution, visible in the next API call's context)
        prefetch_sections = self._consume_prefetch()
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
        if prefetch_sections:
            # §3.10: appended to the END of the history (before normalize) —
            # appending never changes the existing prefix bytes, so the
            # server prefix cache stays intact (review O1: a leading
            # per-turn-changing reminder would break it and make the §3.9
            # detector lie); normalize merges adjacent user messages, so the
            # alternation contract holds. Request view only, never persisted.
            messages = [*messages, user_message(_render_prefetch(prefetch_sections))]
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
        if self._hook_reminder is not None:
            # §7.2:钩子注入的 updatedSystemReminder/additionalContext 作为第三位
            # prefix 消息(context bundle + recovery 之后、历史之前);一次性消费。
            # 内容变化主动打破前缀缓存,§3.9 检测器只记日志(§7.2 预期行为)
            prefix.append(
                Message(
                    role="user",
                    content=f"{REMINDER_HEADER}{self._hook_reminder}{REMINDER_FOOTER}",
                )
            )
            self._hook_reminder = None
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
                    try:
                        self.on_stream(ev)
                    except Exception:
                        # best-effort UI callback — a renderer bug must never
                        # kill the run (same rule as tool_queue._emit_tool_event)
                        logger.exception("on_stream callback failed")
                    yield ev

            stream = _tee()
        response = await LLMClient.collect(stream)
        if self.abort.is_set():
            return None
        if response.is_error and response.error_message and is_ptl_text(response.error_message):
            # adapters surface PTL as a streamed error event, not a raise —
            # convert to the exception shape so the reactive-compaction
            # branch (and its retry-once semantics) engages (review C1)
            raise LLMError(response.error_message, status_code=400)
        # §3.10: kick the prefetch for the NEXT turn before returning — the
        # model's thinking time hides the disk search latency
        self._start_prefetch(messages)
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
            stop_reason=response.stop_reason,
        )

    def _start_prefetch(self, messages: list[SessionMessage]) -> None:
        """Launch the prefetch callback (bounded by PREFETCH_TIMEOUT_S) only
        when no task is outstanding — a search that finished mid-stream but
        was not yet consumed must survive into the next turn, not be
        replaced by a fresh one (review R2: done-but-unconsumed is kept)."""
        if self.prefetch is None or self._prefetch_task is not None:
            return

        async def _bounded():
            try:
                return await asyncio.wait_for(
                    self.prefetch(list(messages)),  # snapshot: the live list grows
                    timeout=PREFETCH_TIMEOUT_S,
                )
            except Exception:  # timeout or callback failure: no sections
                return None

        self._prefetch_task = asyncio.create_task(_bounded())

    def _consume_prefetch(self) -> list[tuple[str, str]] | None:
        """Sections from a completed prefetch; None while it is still running
        (the task survives into the next turn — first-completed-first-injected)."""
        task = self._prefetch_task
        if task is None or not task.done():
            return None
        self._prefetch_task = None
        if task.cancelled():
            return None
        try:
            sections = task.result()
        except Exception:
            return None
        if not sections:
            return None
        return sections[:MAX_REMINDER_SECTIONS]

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
            pre_hook=self._pre_tool_use_hook,  # PreToolUse 钩子(阶段 09 §5.1,先于权限引擎)
            post_hook=self._post_tool_use_hook,  # PostToolUse 钩子(阶段 09 §6.1)
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

    async def _pre_tool_use_hook(self, item: ScheduledTool) -> ToolResult | None:
        """PreToolUse 钩子接线(阶段 09 §5.1/§5.2/§5.4/§5.3),先于权限引擎。

        dispatch 决策合并后落位:updatedInput 改写 item.input(引擎与执行读同一
        字段)、hook_allowed/immune 落位(§5.2/§5.5);deny → 拒绝 ToolResult。
        allow 短路引擎决策链,唯一例外 = 写保护地板(§5.3):降级为
        request_permission 人工确认。返回 None = 继续(引擎照常或 allow 放行)。
        """
        hooks = self.hooks
        if hooks is None or not hooks.has_hooks_for_event("PreToolUse"):
            return None  # 零路径(§4.10.1):未配置钩子 → 引擎照常
        try:
            result = await hooks.dispatch(
                "PreToolUse",
                input=HookInput(
                    session_id=self.session.session_id if self.session else "",
                    cwd=str(self.cwd),
                    session_path=str(self.session.path) if self.session else "",
                    extra={
                        "tool_name": item.tool.name,
                        "tool_input": item.input,
                        "tool_use_id": item.tool_use_id,
                    },
                ),
                abort_event=self.abort,  # §6.3:钩子批次 abort 感知
            )
        except Exception:
            # §6.3:钩子层自身 bug 不拖垮主循环;PreToolUse 下 fail-closed
            # (§4.6 原则:钩子没能说话 → 安全门关闭,拒绝经 permission_blocked
            # 豁免不株连 sibling)。§8.1「每决策恰一条」:异常 deny 也记一条审计。
            logger.exception("PreToolUse hook dispatch failed (fail-closed)")
            self.permissions.audit.emit(
                ToolAuditEvent(
                    tool_name=item.tool.name,
                    decision="deny",
                    reason="PreToolUse hook dispatch error (fail-closed)",
                    source="hook:PreToolUse",
                    mode="default",
                    input_summary=None,  # 钩子输入内容不落审计(§8.1)
                )
            )
            return ToolResult("Permission denied by hook: dispatch error", is_error=True)
        if result.updated_input is not None:
            # §5.4:改写先于引擎求值,引擎与执行读同一字段。deny 终局下改写也已
            # 并入此字段——但工具不执行、会话不落盘,改写无消费面,无害(m3)
            item.input = result.updated_input
        item.hook_allowed = result.hook_allowed  # §5.2:allow 短路位
        item.immune = result.immune  # §5.5:免疫位仅携带 + 审计(v1 无消费面)
        if result.permission_decision == "deny":
            return ToolResult(result.deny_reason or "Permission denied by hook", is_error=True)
        if result.hook_allowed:
            # §5.3:写保护地板是 allow 短路的唯一例外 —— 降级为人工确认
            # (审计 source=write-protection 由 floor_check 经 _decide 既有路径发出)
            floor = self.permissions.floor_check(
                tool_name=item.tool.name, tool_input=item.input, cwd=self.cwd, mode=self.mode
            )
            if floor is not None:
                if self.request_permission is not None and await self.request_permission(
                    floor, item.tool, item.input
                ):
                    return None  # 人工确认 → 放行(引擎不跑:钩子已决策)
                return ToolResult(f"Permission denied: {floor.reason}", is_error=True)
        return None

    async def _post_tool_use_hook(self, item: ScheduledTool, result: ToolResult) -> None:
        """PostToolUse 钩子接线(阶段 09 §6.1):成功与拒绝路径都触发,观察型(§4.10.6)。

        tool_response = 序列化 ToolResult(content + is_error)。钩子失败仅日志,
        不改变工具结果(§6.3 best-effort,不拖垮主循环)。
        """
        hooks = self.hooks
        if hooks is None or not hooks.has_hooks_for_event("PostToolUse"):
            return
        try:
            await hooks.dispatch(
                "PostToolUse",
                input=HookInput(
                    session_id=self.session.session_id if self.session else "",
                    cwd=str(self.cwd),
                    session_path=str(self.session.path) if self.session else "",
                    extra={
                        "tool_name": item.tool.name,
                        "tool_input": item.input,
                        "tool_use_id": item.tool_use_id,
                        "tool_response": {"content": result.content, "is_error": result.is_error},
                    },
                ),
                abort_event=self.abort,
            )
        except Exception:
            logger.exception("PostToolUse hook dispatch failed")

    async def _dispatch_session_start(self) -> None:
        """SessionStart 钩子(§6.2):run() 入口,门闩一次(调用方已判位)。

        HookInput 独有字段(§2.2):source(startup/resume,history 非空即 resume)、
        model。非阻塞(§4.6 表):exit 2 忽略(§4.3),异常仅日志,不拖垮启动。
        additionalContext 累积进一次性 _hook_reminder(§7.1)。
        """
        hooks = self.hooks
        if hooks is None or not hooks.has_hooks_for_event("SessionStart"):
            return
        try:
            result = await hooks.dispatch(
                "SessionStart",
                input=HookInput(
                    session_id=self.session.session_id if self.session else "",
                    cwd=str(self.cwd),
                    session_path=str(self.session.path) if self.session else "",
                    extra={
                        "source": "resume" if self.history else "startup",
                        "model": self.model,
                    },
                ),
                abort_event=self.abort,  # §6.3:钩子批次 abort 感知
            )
        except Exception:
            logger.exception("SessionStart hook dispatch failed (non-blocking, §4.6)")
            return
        self._accumulate_hook_reminder(result.additional_context)

    async def _dispatch_user_prompt(
        self, user_input: str | list[ContentBlock]
    ) -> tuple[str | list[ContentBlock] | None, str | None]:
        """UserPromptSubmit 钩子(§6.2):每条用户输入,user_message() 之前(fail-open)。

        返回 (改写后输入, blocking_error):blocking_error 非 None = exit 2 阻止提交
        (§4.3 输入擦除,调用方终结运行或静默丢弃);updatedPrompt 改写输入文本(§7.1);
        updatedSystemReminder/additionalContext 累积进一次性 _hook_reminder(§7.2)。
        thinking-only 恢复消息不是用户输入,不触发(§2.2 边界)。
        """
        hooks = self.hooks
        if hooks is None or not hooks.has_hooks_for_event("UserPromptSubmit"):
            return user_input, None
        # 原文进 HookInput.prompt(§2.2);块输入序列化为 dict(ContentBlock 不可直接 JSON)
        prompt = (
            user_input
            if isinstance(user_input, str)
            else [b.model_dump() for b in user_input]
        )
        try:
            result = await hooks.dispatch(
                "UserPromptSubmit",
                input=HookInput(
                    session_id=self.session.session_id if self.session else "",
                    cwd=str(self.cwd),
                    session_path=str(self.session.path) if self.session else "",
                    extra={"prompt": prompt},
                ),
                abort_event=self.abort,
            )
        except Exception:
            # 非阻塞(§4.6 表:非 PreToolUse 事件 fail-open);输入照常进入循环
            logger.exception("UserPromptSubmit hook dispatch failed (non-blocking)")
            return user_input, None
        self._accumulate_hook_reminder(result.updated_system_reminder)
        self._accumulate_hook_reminder(result.additional_context)
        if result.blocking_error is not None:
            return None, result.blocking_error
        if result.updated_prompt is not None:
            return result.updated_prompt, None  # §7.1:改写后文本即为本次对话真实输入
        return user_input, None

    async def _dispatch_stop(
        self, reason: str, assistant: SessionMessage
    ) -> HookDispatchResult | None:
        """Stop 钩子(§6.4 门控):completed/tool_terminated 两分支,return/_stop 之前。

        None = 未配置 Stop 钩子或 dispatch 异常(fail-open,照常停止,CC 同款)。
        HookInput 独有字段(§2.2):reason、last_assistant_message(文本/块序列化)。
        消费方(§6.4 + S5 m2):result.stop(显式 continue:false)优先于
        result.stop_feedback(exit 2);两字段并存时按显式指令停止。
        """
        hooks = self.hooks
        if hooks is None or not hooks.has_hooks_for_event("Stop"):
            return None
        content = (
            assistant.content
            if isinstance(assistant.content, str)
            else [b.model_dump() for b in assistant.content]
        )
        try:
            return await hooks.dispatch(
                "Stop",
                input=HookInput(
                    session_id=self.session.session_id if self.session else "",
                    cwd=str(self.cwd),
                    session_path=str(self.session.path) if self.session else "",
                    extra={"reason": reason, "last_assistant_message": content},
                ),
                abort_event=self.abort,
            )
        except Exception:
            # §6.4:钩子层自身异常 → 只警告 + 日志,不影响停止(CC fail-open)
            logger.exception("Stop hook dispatch failed (fail-open, §6.4)")
            return None

    def _allow_stop_feedback(self) -> bool:
        """M1(§6.4 补):Stop feedback 注入上限 —— 达 MAX_STOP_HOOK_ATTEMPTS(5)
        后不再注入,按普通 completed/tool_terminated 停止,不报错。

        计数按 run() 生命周期(run() 入口重置);只在真正注入时递增,
        防「永远 exit 2」的钩子拖出无限循环(对齐 CC MAX_STOP_HOOK_ATTEMPTS)。
        """
        if self._stop_feedback_count >= MAX_STOP_HOOK_ATTEMPTS:
            return False
        self._stop_feedback_count += 1
        return True

    def _accumulate_hook_reminder(self, text: str | None) -> None:
        """§7.1/§7.2:additionalContext/updatedSystemReminder 累积进一次性 prefix。

        多钩子/多事件输出顺序 join('\n\n')(§4.10.6 聚合传递链);注入后由
        _ask_model 消费并清除。
        """
        if not text:
            return
        self._hook_reminder = f"{self._hook_reminder}\n\n{text}" if self._hook_reminder else text

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


def _render_prefetch(sections: list[tuple[str, str]]) -> str:
    """§3.10 prefetch sections as one system-reminder text block (same channel
    as the context bundle, capped at MAX_REMINDER_SECTIONS)."""
    parts = [REMINDER_HEADER]
    for title, text in sections:
        parts.append(f"# {title}\n{text}\n")
    parts.append(REMINDER_FOOTER)
    return "".join(parts)


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
