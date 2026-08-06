"""Write tool: create or overwrite a file (atomic)."""

from __future__ import annotations

from ....config import atomic_write
from ...base import Tool, ToolResult, ToolUseContext
from ._common import ensure_read_freshness, record_written, resolve_path


class WriteTool(Tool):
    name = "Write"
    description = (
        "Write (create or overwrite) a file; parent directories are created. "
        "An existing file must be Read first; Write replaces the whole content — "
        "prefer Edit for small changes."
    )
    input_schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["file_path", "content"],
    }
    is_concurrency_safe = False

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        path = resolve_path(ctx, str(input["file_path"]))
        content = str(input["content"])
        if path.exists():
            # creating a new file is fine; overwriting an unread one is not
            if path not in ctx.read_file_timestamps:
                return ToolResult(
                    f"File already exists; Read it first before overwriting ({path})", is_error=True
                )
            stale = ensure_read_freshness(ctx, path)
            if stale:
                return ToolResult(stale, is_error=True)
        try:
            atomic_write(path, content)
        except OSError as exc:
            return ToolResult(f"Error writing {path}: {exc}", is_error=True)
        record_written(ctx, path, content)
        return ToolResult(f"Wrote {len(content)} bytes to {path}")
