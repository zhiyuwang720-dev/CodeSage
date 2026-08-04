"""LS tool: list directory contents."""

from __future__ import annotations

from ...base import Tool, ToolResult, ToolUseContext
from ._common import resolve_path


class LSTool(Tool):
    name = "LS"
    description = "List directory contents (names and types only)."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory to list (default: cwd)"}},
    }
    is_concurrency_safe = True

    def needs_permissions(self, input: dict) -> bool:
        return False  # read-only; permission engine handles sensitive paths

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        path = resolve_path(ctx, str(input.get("path") or "."))
        if not path.is_dir():
            return ToolResult(f"Error: {path} is not a directory", is_error=True)
        entries = []
        for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            kind = "dir" if entry.is_dir() else "file"
            entries.append(f"{entry.name}/" if kind == "dir" else entry.name)
        return ToolResult("\n".join(entries) if entries else "(empty)")
