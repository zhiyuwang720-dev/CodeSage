"""Write tool: create or overwrite a file (atomic)."""

from __future__ import annotations

from ....config import atomic_write
from ...base import Tool, ToolResult, ToolUseContext
from ._common import resolve_path


class WriteTool(Tool):
    name = "Write"
    description = "Write (create or overwrite) a file; parent directories are created."
    input_schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["file_path", "content"],
    }
    is_concurrency_safe = False

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        path = resolve_path(ctx, str(input["file_path"]))
        content = str(input["content"])
        try:
            atomic_write(path, content)
        except OSError as exc:
            return ToolResult(f"Error writing {path}: {exc}", is_error=True)
        return ToolResult(f"Wrote {len(content)} bytes to {path}")
