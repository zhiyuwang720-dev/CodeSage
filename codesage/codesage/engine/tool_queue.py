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

from ..tools import ToolResult, ToolUseContext

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
        if item.context.abort_event is not None and item.context.abort_event.is_set():
            return ToolResult("(interrupted by user)", is_error=True)
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
        # empty tool_result risks the model reading "no output" as a stop signal;
        # give it an explicit no-output marker instead (CC-03)
        if not result.is_error and result.content in ("", []):
            result = ToolResult(
                content=f"({item.tool.name} completed with no output)",
                is_error=result.is_error,
                new_messages=result.new_messages,
                metadata=result.metadata,
            )
        result = _spill_large_result(result, item.tool_use_id)
        if self._post_hook is not None:
            await self._post_hook(item, result)
        return result
