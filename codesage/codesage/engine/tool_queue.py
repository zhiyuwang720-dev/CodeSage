"""ToolUseQueue: sibling tool scheduling (design note #3).

Concurrency-safe tools run in parallel; a non-safe tool acts as a barrier
(only it runs, sequentially). If any tool in a batch errors, the remaining
siblings receive a synthesized <tool_use_error> result instead of running —
the model sees the failure and self-heals.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools import ToolError, ToolResult, ToolUseContext

SIBLING_ERROR_TEXT = "<tool_use_error>Sibling tool call errored</tool_use_error>"
#: str results larger than this are spilled to a temp file; the model sees a pointer.
MAX_TOOL_RESULT_CHARS = 100_000
RESULT_PREVIEW_CHARS = 500


#: tool_use_id -> spill path; deterministic reuse keeps prompt-cache prefixes stable.
_spill_cache: dict[str, Path] = {}
#: Fixed prefix under the temp dir (session-level grouping left to callers).
_SPILL_PREFIX = "codesage"


def _spill_large_result(result: ToolResult, tool_use_id: str = "") -> ToolResult:
    """Persist oversized str results to disk; replace content with a file pointer.

    The path is deterministic per tool_use_id: re-spilling the same id with
    identical content reuses the file (no rewrite); different content overwrites
    the same path (path stays stable across replays).
    """
    content = result.content
    if not isinstance(content, str) or len(content) <= MAX_TOOL_RESULT_CHARS:
        return result
    path = Path(tempfile.gettempdir()) / "tool-results" / _SPILL_PREFIX / f"{tool_use_id or 'x'}.txt"
    if _spill_cache.get(tool_use_id) == path and path.exists() and path.read_text(encoding="utf-8") == content:
        pass  # identical replay — reuse the existing file
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _spill_cache[tool_use_id] = path
    return ToolResult(
        content=f"(result saved to {path}: {content[:RESULT_PREVIEW_CHARS]}...)",
        is_error=result.is_error,
        new_messages=result.new_messages,
        metadata=result.metadata,
        terminate=result.terminate,
    )


@dataclass(slots=True)
class ScheduledTool:
    """One tool invocation scheduled by the queue."""

    tool_use_id: str
    tool: Any
    input: dict[str, Any]
    context: ToolUseContext
    status: str = "queued"  # queued | executing | completed | yielded
    result: ToolResult | None = None
    # 阶段 09 §5.2/§5.5:PreToolUse 钩子决策落位 —— allow 短路位(引擎决策链
    # 不运行)与 safetyCheck bypass 免疫位(v1 仅携带 + 审计,无消费面)
    hook_allowed: bool = False
    immune: bool = False


class ToolUseQueue:
    """Executes sibling tool calls with concurrency-safety barriers."""

    def __init__(
        self,
        tools: list[ScheduledTool],
        *,
        permission_check: Any | None = None,  # async (ScheduledTool) -> ToolResult | None
        pre_hook: Any | None = None,  # async (ScheduledTool) -> ToolResult | None (阶段 09 §5.1:拒绝返回 ToolResult,否则 None)
        post_hook: Any | None = None,  # async (ScheduledTool, ToolResult) -> None (阶段 09 §6.1)
        finalize: Any | None = None,  # async (ScheduledTool, ToolResult) -> ToolResult (PI-02)
        on_tool_event: Any | None = None,  # (event, tool_name, payload) lifecycle (PI-01)
        notify: Any | None = None,  # async (notification_type, message, **data) — 阶段 09 §2.5 通知 emit
    ):
        self._tools = tools
        self._permission_check = permission_check
        self._pre_hook = pre_hook
        self._post_hook = post_hook
        self._finalize = finalize
        self._on_tool_event = on_tool_event
        self._notify = notify

    def _emit_tool_event(self, event: str, tool_name: str, payload: dict) -> None:
        """Fire a lifecycle event; a misbehaving callback must never break tools."""
        if self._on_tool_event is None:
            return
        try:
            self._on_tool_event(event, tool_name, payload)
        except Exception:
            # UI/telemetry callbacks are best-effort; log and continue
            import logging

            logging.getLogger("codesage.engine").warning(
                "on_tool_event %s(%s) failed", event, tool_name, exc_info=True
            )

    async def run(self) -> list[ScheduledTool]:
        """Execute all scheduled tools; returns them with results attached."""
        pending = list(self._tools)
        index = 0
        while index < len(pending):
            # slice the next batch: consecutive safe tools, stopped by a barrier
            # (items that already carry a result, e.g. unknown tools, are done)
            batch: list[ScheduledTool] = []
            while index < len(pending):
                item = pending[index]
                if item.status != "queued":
                    index += 1
                    continue
                batch.append(item)
                index += 1
                if not item.tool.is_concurrency_safe:
                    break

            results = await asyncio.gather(
                *(self._execute(item) for item in batch), return_exceptions=True
            )
            # a permission denial is the USER's decision on one tool, not an
            # execution error — siblings must still run their own permission
            # gate (CC semantics), so it never triggers the sibling policy
            any_error = any(
                isinstance(r, BaseException)
                or (
                    r is not None
                    and r.is_error
                    and r.metadata.get("error_code") != "permission_blocked"
                )
                for r in results
            )
            # apply results
            for item, result in zip(batch, results):
                if isinstance(result, BaseException):
                    item.result = ToolResult(str(result), is_error=True)
                elif result is not None:
                    item.result = result
                item.status = "completed"
            if any_error:
                # Sibling policy (Kode design note #3, softened per CC review):
                # completed siblings keep their real results — voiding a
                # successful Read/Grep because a sibling failed throws away
                # useful signal. Only tools that have NOT started yet are
                # voided (a failed write may have corrupted the environment,
                # so speculative siblings must not run).
                for item in pending[index:]:
                    item.result = ToolResult(SIBLING_ERROR_TEXT, is_error=True)
                    item.status = "completed"
                break
        return self._tools

    async def _execute(self, item: ScheduledTool) -> ToolResult:
        # ---- prepare: abort / 钩子层 / permission gate (阶段 09 §5.1) ----
        if item.context.abort_event is not None and item.context.abort_event.is_set():
            return ToolResult("(interrupted by user)", is_error=True)
        item.status = "executing"
        if self._pre_hook is not None:
            denied = await self._pre_hook(item)  # 钩子层:决策合并 + updatedInput/immune 落位
            if denied is not None:  # 钩子 deny → 直接拒绝(复用 permission_blocked 豁免)
                denied.metadata.setdefault("error_code", "permission_blocked")
                if self._post_hook is not None:
                    await self._post_hook(item, denied)
                return denied
        if not item.hook_allowed and self._permission_check is not None:  # allow 短路:跳过引擎
            denied = await self._permission_check(item)
            if denied is not None:
                denied.metadata.setdefault("error_code", "permission_blocked")
                if self._post_hook is not None:
                    await self._post_hook(item, denied)
                return denied

        # ---- execute (PI-01: lifecycle events) ----
        self._emit_tool_event("start", item.tool.name, {"tool_use_id": item.tool_use_id})
        try:
            result = None
            async for partial in item.tool.call(item.input, item.context):
                result = partial  # last yielded value is the ToolResult
                if self._on_tool_event is not None and hasattr(partial, "text") and not hasattr(partial, "content"):
                    # ToolProgress: carry the progress text (PI-01 update event)
                    self._emit_tool_event(
                        "update", item.tool.name,
                        {"tool_use_id": item.tool_use_id, "progress": getattr(partial, "text", "")},
                    )
        except ToolError as exc:
            result = ToolResult(str(exc), is_error=True, metadata={"error_code": exc.code} if exc.code else {})
            # 通知(§2.5):tool_error fail-open emit —— 失败仅日志,不拖累工具错误路径
            if self._notify is not None:
                await self._notify(
                    "tool_error",
                    f"Tool error: {item.tool.name}: {exc}",
                    tool_name=item.tool.name,
                    error_code=exc.code if exc.code else None,  # 与 ToolResult metadata 同策略
                )
        finally:
            self._emit_tool_event("end", item.tool.name, {"tool_use_id": item.tool_use_id})
        if result is None:
            result = ToolResult("(no result)", is_error=True)

        # ---- finalize: no-output marker, spill, rewrite hook (PI-02 last phase) ----
        if not result.is_error and result.content in ("", []):
            result = ToolResult(
                content=f"({item.tool.name} completed with no output)",
                is_error=result.is_error,
                new_messages=result.new_messages,
                metadata=result.metadata,
                terminate=result.terminate,
            )
        result = _spill_large_result(result, item.tool_use_id)
        if self._finalize is not None:
            result = await self._finalize(item, result)
        if self._post_hook is not None:
            await self._post_hook(item, result)
        return result
