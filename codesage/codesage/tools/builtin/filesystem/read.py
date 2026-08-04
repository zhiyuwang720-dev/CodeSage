"""Read tool: read a text file with line numbers, offset, limit."""

from __future__ import annotations

from ...base import Tool, ToolResult, ToolUseContext
from ._common import decode_text, is_binary, resolve_path

DEFAULT_READ_LINES = 2000
MAX_READ_LINES = 20000


class ReadTool(Tool):
    name = "Read"
    description = "Read a text file, optionally with line numbers and an offset/limit."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "offset": {"type": "integer", "description": "0-based start line (default 0)"},
            "limit": {"type": "integer", "description": "Max lines (default 2000, cap 20000)"},
        },
        "required": ["file_path"],
    }
    is_concurrency_safe = True

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        path = resolve_path(ctx, str(input["file_path"]))
        if not path.is_file():
            return ToolResult(f"Error: {path} does not exist or is not a file", is_error=True)
        if is_binary(path):
            return ToolResult(f"Error: {path} appears to be a binary file; Read only handles text", is_error=True)
        offset = int(input.get("offset") or 0)
        limit = int(input.get("limit") or DEFAULT_READ_LINES)
        limit = min(max(limit, 1), MAX_READ_LINES)
        try:
            data = path.read_bytes()
        except OSError as exc:
            return ToolResult(f"Error reading {path}: {exc}", is_error=True)
        text = decode_text(data)
        lines = text.splitlines()
        if offset >= len(lines):
            return ToolResult(f"Error: offset {offset} beyond file length {len(lines)}", is_error=True)
        window = lines[offset : offset + limit]
        numbered = "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(window))
        truncated = len(lines) > offset + limit
        suffix = f"\n(truncated: showing lines {offset + 1}-{offset + len(window)} of {len(lines)})" if truncated else ""
        return ToolResult(numbered + suffix)
