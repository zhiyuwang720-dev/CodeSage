"""TaskList: summary lines of all tasks in the active task list."""

from __future__ import annotations

from typing import Any

from ...base import Tool, ToolResult, ToolUseContext
from ....core.tasks import TaskStoreError, TaskSummary, get_task_store


class TaskListTool(Tool):
    name = "TaskList"
    description = ("List task summaries in the active task list, one line per "
                   "task in id order: \"#3 [pending] Fix auth (alice) [blocked by #1, #2]\".")
    input_schema = {
        "type": "object",
        "properties": {},
    }
    is_concurrency_safe = True  # read-only
    user_facing_name = "TaskList"

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        return False  # harness-internal state; whitelisted in SYSTEM_TOOLS

    @staticmethod
    def _format_summary(s: TaskSummary) -> str:
        line = f"#{s.id} [{s.status.value}] {s.subject}"
        if s.owner:
            line += f" ({s.owner})"
        if s.blocked_by:
            line += f" [blocked by #{', #'.join(s.blocked_by)}]"
        return line

    async def _run(self, input: dict[str, Any], ctx: ToolUseContext) -> ToolResult:
        try:
            summaries = get_task_store().summaries(ctx.task_list_id)  # 只读,同步
        except TaskStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        if not summaries:
            return ToolResult("No tasks found")
        return ToolResult("\n".join(self._format_summary(s) for s in summaries))
