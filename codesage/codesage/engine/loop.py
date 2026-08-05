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
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from ..ai import ContentBlock, LLMClient, LLMError, LLMRequest, StreamEvent
from ..core import Session, SessionMessage, assistant_message, normalize_for_api, user_message
from ..permissions import PermissionDecision, PermissionEngine, PermissionMode
from ..permissions.store import load_permission_rules
from ..tools import ToolError, ToolRegistry, ToolResult, ToolUseContext
from .system_prompt import build_system_prompt
from .tool_queue import ScheduledTool, ToolUseQueue

#: Synthesized messages (is_meta — filtered by normalize_for_api).
INTERRUPT_TEXT = "(interrupted by user)"
MAX_TURNS_TEXT = "Stopped: maximum turn count reached."
MAX_BUDGET_TEXT = "Stopped: maximum budget reached."
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
        context: dict[str, str] | None = None,
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
    ):
        self.client = client
        self.tools = tools
        self.permissions = permissions or PermissionEngine()
        self.request_permission = request_permission
        self.system_prompt = system_prompt
        self.context = context
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
        #: Termination reason of the last run(): "completed" | "max_turns" |
        #: "max_budget" | "interrupted" | "error" | "thinking_only_exhausted" (CC-10)
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

                # LLM call (checkpoint 1: abort before and after)
                assistant = await self._ask_model(messages)
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

    async def _ask_model(self, messages: list[SessionMessage]) -> SessionMessage | None:
        request = LLMRequest(
            messages=normalize_for_api(messages),
            system=build_system_prompt(self.system_prompt, self.context),
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
        queue = ToolUseQueue(scheduled, permission_check=self._permission_check)
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


class _MissingTool:
    """Placeholder for unknown tool names; reports itself as failed."""

    is_concurrency_safe = True

    def __init__(self, name: str):
        self.name = name

    def needs_permissions(self, input: dict) -> bool:
        return False
