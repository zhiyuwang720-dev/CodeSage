"""TaskGet: fetch one task's full details as single-line JSON."""

from __future__ import annotations

from typing import Any

from ...base import Tool, ToolError, ToolResult, ToolUseContext
from ....core.tasks import TaskStoreError, get_task_store


class TaskGetTool(Tool):
    name = "TaskGet"
    description = ("Get one task's full details as JSON (subject, description, "
                   "status, owner, blocks, blockedBy, metadata).")
    input_schema = {
        "type": "object",
        "properties": {
            "taskId": {"type": "string", "description": "Task id, e.g. \"3\""},
        },
        "required": ["taskId"],
    }
    is_concurrency_safe = True  # read-only
    user_facing_name = "TaskGet"

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        return False  # harness-internal state; whitelisted in SYSTEM_TOOLS

    def validate_input(self, input: dict[str, Any]) -> None:
        if not str(input.get("taskId", "")).strip():
            raise ToolError("taskId is required")

    async def _run(self, input: dict[str, Any], ctx: ToolUseContext) -> ToolResult:
        task_id = input["taskId"]
        try:
            task = get_task_store().get(ctx.task_list_id, task_id)  # 只读,同步
        except TaskStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        if task is None:
            return ToolResult(f"Task not found: {task_id}", is_error=True)
        return ToolResult(task.model_dump_json())
