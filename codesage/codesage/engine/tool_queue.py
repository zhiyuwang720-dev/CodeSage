"""ToolUseQueue: sibling tool scheduling (design note #3).

Concurrency-safe tools run in parallel; a non-safe tool acts as a barrier
(only it runs, sequentially). If any tool in a batch errors, the remaining
siblings receive a synthesized <tool_use_error> result instead of running —
the model sees the failure and self-heals.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools import ToolResult, ToolUseContext

SIBLING_ERROR_TEXT = "<tool_use_error>Sibling tool call errored</tool_use_error>"
#: str results larger than this are spilled to a temp file; the model sees a pointer.
MAX_TOOL_RESULT_CHARS = 100_000
RESULT_PREVIEW_CHARS = 500


def _spill_large_result(result: ToolResult) -> ToolResult:
    """Persist oversized str results to disk; replace content with a file pointer."""
    content = result.content
    if not isinstance(content, str) or len(content) <= MAX_TOOL_RESULT_CHARS:
        return result
    path = Path(tempfile.mkdtemp(prefix="codesage-tool-")) / f"tool-result-{uuid.uuid4().hex}.txt"
    path.write_text(content, encoding="utf-8")
    return ToolResult(
        content=f"(result saved to {path}: {content[:RESULT_PREVIEW_CHARS]}...)",
        is_error=result.is_error,
        new_messages=result.new_messages,
        metadata=result.metadata,
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


class ToolUseQueue:
    """Executes sibling tool calls with concurrency-safety barriers."""

    def __init__(
        self,
        tools: list[ScheduledTool],
        *,
        permission_check: Any | None = None,  # async (ScheduledTool) -> ToolResult | None
        pre_hook: Any | None = None,  # async (ScheduledTool) -> None (phase 09)
        post_hook: Any | None = None,  # async (ScheduledTool, ToolResult) -> None (phase 09)
    ):
        self._tools = tools
        self._permission_check = permission_check
        self._pre_hook = pre_hook
        self._post_hook = post_hook

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
            any_error = any(isinstance(r, BaseException) or (r is not None and r.is_error) for r in results)
            # apply results
            for item, result in zip(batch, results):
                if isinstance(result, BaseException):
                    item.result = ToolResult(str(result), is_error=True)
                elif result is not None:
                    item.result = result
                item.status = "completed"
            if any_error:
                # a failed batch voids its own non-error siblings (design note #3)
                for item in batch:
                    if not item.result.is_error:
                        item.result = ToolResult(SIBLING_ERROR_TEXT, is_error=True)
                # ...and poisons everything still queued
                for item in pending[index:]:
                    item.result = ToolResult(SIBLING_ERROR_TEXT, is_error=True)
                    item.status = "completed"
                break
        return self._tools

    async def _execute(self, item: ScheduledTool) -> ToolResult:
        item.status = "executing"
        if self._pre_hook is not None:
            await self._pre_hook(item)
        if self._permission_check is not None:
            denied = await self._permission_check(item)
            if denied is not None:
                if self._post_hook is not None:
                    await self._post_hook(item, denied)
                return denied
        result = None
        async for partial in item.tool.call(item.input, item.context):
            result = partial  # last yielded value is the ToolResult
        if result is None:
            result = ToolResult("(no result)", is_error=True)
        result = _spill_large_result(result)
        if self._post_hook is not None:
            await self._post_hook(item, result)
        return result
