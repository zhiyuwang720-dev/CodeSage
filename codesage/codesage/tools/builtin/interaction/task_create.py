"""TaskCreate: create a persistent task in the active task list."""

from __future__ import annotations

from typing import Any

from ...base import Tool, ToolError, ToolResult, ToolUseContext
from ....core.tasks import TaskStoreError, get_task_store


class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = ("Create a task for multi-step work. Use an imperative subject "
                   "(\"Fix auth bug\" not \"Fixing auth bug\"); description must be "
                   "detailed enough for another agent to pick it up.")
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Imperative title"},
            "description": {"type": "string", "description": "Detail for handoff"},
            "activeForm": {"type": "string", "description": "Present-continuous spinner form"},
            "metadata": {"type": "object"},
        },
        "required": ["subject", "description"],
    }
    is_concurrency_safe = False  # mutates the store
    user_facing_name = "TaskCreate"

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        return False  # harness-internal state; whitelisted in SYSTEM_TOOLS

    def validate_input(self, input: dict[str, Any]) -> None:
        # 与存储层同一文案(§6.5:校验先抛,存储层仍自守);
        # 显式 None/非 str 也拒绝 —— str(None) 会以 "None" 字符串入库(P3-2)
        for field in ("subject", "description"):
            value = input.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ToolError("subject and description are required")

    async def _run(self, input: dict[str, Any], ctx: ToolUseContext) -> ToolResult:
        try:
            task = await get_task_store().create(
                ctx.task_list_id,
                subject=str(input["subject"]).strip(),
                description=str(input["description"]).strip(),
                active_form=input.get("activeForm"),
                metadata=input.get("metadata"),
            )
        except TaskStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"Created task #{task.id}: {task.subject}")
