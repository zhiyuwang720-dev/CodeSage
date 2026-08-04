"""Edit tool: structured string replacement with uniqueness guard."""

from __future__ import annotations

from ....config import atomic_write
from ...base import Tool, ToolResult, ToolUseContext
from ._common import resolve_path


class EditTool(Tool):
    name = "Edit"
    description = "Replace old_string with new_string in a file (exact match, unique or replace_all)."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
        },
        "required": ["file_path", "old_string", "new_string"],
    }
    is_concurrency_safe = False

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        path = resolve_path(ctx, str(input["file_path"]))
        if not path.is_file():
            return ToolResult(f"Error: {path} does not exist", is_error=True)
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(f"Error reading {path}: {exc}", is_error=True)
        old, new = str(input["old_string"]), str(input["new_string"])
        if old not in original:
            return ToolResult(
                f"Error: old_string not found in {path}. Hint: the file may have changed; re-Read it.",
                is_error=True,
            )
        if not input.get("replace_all") and original.count(old) > 1:
            return ToolResult(
                f"Error: old_string appears {original.count(old)} times; set replace_all=true or make it unique.",
                is_error=True,
            )
        updated = original.replace(old, new)
        atomic_write(path, updated)
        return ToolResult(f"Edited {path} ({original.count(old)} replacement(s))")
