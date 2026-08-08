"""TaskUpdate: update fields/status/dependencies, or delete (status="deleted")."""

from __future__ import annotations

from typing import Any

from ...base import Tool, ToolError, ToolResult, ToolUseContext
from ....core.tasks import TaskStoreError, TaskUpdate, get_task_store

#: 输出标注的字段顺序(镜像 Kode updatedFields 顺序);块/元数据为集合值,只标名
_FIELD_DISPLAY = (("subject", "subject"), ("description", "description"),
                  ("active_form", "activeForm"), ("owner", "owner"),
                  ("status", "status"), ("metadata", "metadata"),
                  ("add_blocks", "blocks"), ("add_blocked_by", "blockedBy"))


def _format_updated_fields(fields: dict[str, Any]) -> str:
    """按实际变化字段标注:如 "(status → in_progress)"、"(owner → alice)";无变化 → "(ok)"。"""
    parts = []
    for snake, display in _FIELD_DISPLAY:
        if snake not in fields:
            continue
        value = fields[snake]
        if snake in ("metadata", "add_blocks", "add_blocked_by"):
            parts.append(display)
        else:
            parts.append(f"{display} → {value}")
    return ", ".join(parts) if parts else "ok"


class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = ("Update a task: fields, status, or dependencies (add-only). "
                   "status=\"deleted\" permanently deletes the task; a completed "
                   "task cannot be reopened.")
    input_schema = {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "activeForm": {"type": "string"},
            "status": {"enum": ["pending", "in_progress", "completed", "deleted"]},
            "addBlocks": {"type": "array", "items": {"type": "string"}},  # 本任务阻塞的
            "addBlockedBy": {"type": "array", "items": {"type": "string"}},  # 阻塞本任务的
            "owner": {"type": "string"},
            "metadata": {"type": "object"},  # 键值合并;值传 null 删除键(镜像 Kode §6.3)
        },
        "required": ["taskId"],
    }
    is_concurrency_safe = False  # mutates the store
    user_facing_name = "TaskUpdate"

    def needs_permissions(self, input: dict[str, Any]) -> bool:
        return False  # harness-internal state; whitelisted in SYSTEM_TOOLS

    def validate_input(self, input: dict[str, Any]) -> None:
        if not str(input.get("taskId", "")).strip():
            raise ToolError("taskId is required")
        # 同 P3-2 防线:显式出现的 subject/description 必须非空字符串,
        # 防 str(None) → "None" 入库;未传(可选字段)不在此列
        for field in ("subject", "description"):
            if field in input and (not isinstance(input[field], str) or not input[field].strip()):
                raise ToolError(f"{field} must be a non-empty string")
        # §6.3:addBlocks 与 addBlockedBy 同时含任务自身 → 拒绝
        if input.get("addBlocks") and input.get("addBlockedBy"):
            if input["taskId"] in input["addBlocks"] and input["taskId"] in input["addBlockedBy"]:
                raise ToolError(f"Task #{input['taskId']} cannot depend on itself")

    async def _run(self, input: dict[str, Any], ctx: ToolUseContext) -> ToolResult:
        task_id = input["taskId"]
        store = get_task_store()
        if input.get("status") == "deleted":
            try:
                await store.delete(ctx.task_list_id, task_id)
            except TaskStoreError as exc:
                return ToolResult(str(exc), is_error=True)
            return ToolResult(f"Deleted task #{task_id}")
        # 仅传显式出现的字段(camelCase → snake_case),区分「未传」与「显式 None」
        fields: dict[str, Any] = {}
        for camel, snake in (("subject", "subject"), ("description", "description"),
                             ("activeForm", "active_form"), ("status", "status"),
                             ("addBlocks", "add_blocks"), ("addBlockedBy", "add_blocked_by"),
                             ("owner", "owner"), ("metadata", "metadata")):
            if camel in input:
                fields[snake] = str(input[camel]).strip() if camel in ("subject", "description") else input[camel]
        update = TaskUpdate(task_id=task_id, **fields)
        try:
            task = await store.update(ctx.task_list_id, update)
        except TaskStoreError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"Updated task #{task.id} ({_format_updated_fields(fields)})")
