"""TodoWrite tool: session-level todo list (in-memory, idempotent replace).

Kode's TodoWrite persists an agent-scoped todo file; here the store is a
module-level dict. The input list *is* the new list — calling TodoWrite
replaces the previous one, so updates are idempotent by construction.

# ponytail: single process-global store keyed "default"; a per-session key
# lands with the engine's session layer (phase 06).
"""

from __future__ import annotations

from ...base import Tool, ToolError, ToolResult, ToolUseContext

VALID_STATUSES = ("pending", "in_progress", "completed")

#: session key -> todo list; module-level, resets with the process.
_STORE: dict[str, list[dict]] = {"default": []}


def reset_todos(key: str = "default") -> None:
    """Test/engine hook: clear the stored list for a session key."""
    _STORE[key] = []


def _normalize_todos(todos: list) -> list[dict]:
    """Accept list[str] (pending) or list[dict]; validate statuses and the
    single-in_progress invariant. Raises ToolError on invalid input."""
    normalized: list[dict] = []
    for todo in todos:
        if isinstance(todo, str):
            todo = {"content": todo, "status": "pending"}
        if not isinstance(todo, dict):
            raise ToolError(f"Invalid todo entry: {todo!r}")
        content = str(todo.get("content") or "").strip()
        if not content:
            raise ToolError("Todo has empty content")
        status = todo.get("status", "pending")
        if status not in VALID_STATUSES:
            raise ToolError(f'Invalid status "{status}" for todo "{content}"')
        entry = {"content": content, "status": status}
        for key in ("priority", "tags", "estimated_hours"):
            if key in todo:
                entry[key] = todo[key]
        normalized.append(entry)
    if sum(1 for t in normalized if t["status"] == "in_progress") > 1:
        raise ToolError("Only one task can be in_progress at a time")
    return normalized


def _summary(todos: list[dict]) -> str:
    total = len(todos)
    completed = sum(1 for t in todos if t["status"] == "completed")
    in_progress = [t["content"] for t in todos if t["status"] == "in_progress"]
    if in_progress:
        return f"{completed}/{total} 完成 · {len(in_progress)} 进行中: {', '.join(in_progress)}"
    return f"{completed}/{total} 完成"


class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = "Create and update a todo list for this session. Pass the full new list each time — it replaces the previous list. Entries are strings or {content, status: pending|in_progress|completed, priority?, tags?, estimated_hours?}; at most one task may be in_progress."
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Full new todo list",
                "items": {},
            },
        },
        "required": ["todos"],
    }
    is_concurrency_safe = False  # mutates shared state
    user_facing_name = "TodoWrite"

    def needs_permissions(self, input: dict) -> bool:
        return False  # internal session state; already in permissions SYSTEM_TOOLS

    def validate_input(self, input: dict) -> None:
        todos = input.get("todos")
        if not isinstance(todos, list):
            raise ToolError("todos must be a list")
        _normalize_todos(todos)  # raises ToolError on invalid entries

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        try:
            todos = _normalize_todos(input["todos"])
        except ToolError as exc:
            return ToolResult(str(exc), is_error=True)
        _STORE["default"] = todos
        return ToolResult(_summary(todos))
