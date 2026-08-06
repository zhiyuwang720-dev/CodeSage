"""Glob tool: find files by pattern (ripgrep-style ignored dirs)."""

from __future__ import annotations

from ...base import Tool, ToolResult, ToolUseContext
from ._common import MAX_RESULTS, SKIP_DIRS, resolve_root


class GlobTool(Tool):
    name = "Glob"
    description = (
        "Find files by glob pattern (e.g. **/*.py) under a directory, defaulting to the working "
        "directory; ignores common junk dirs (.git, node_modules, __pycache__)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. **/*.py"},
            "path": {"type": "string", "description": "Root directory (default: cwd)"},
        },
        "required": ["pattern"],
    }
    is_concurrency_safe = True

    def needs_permissions(self, input: dict) -> bool:
        return False  # read-only

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        root = resolve_root(ctx, input.get("path"))
        pattern = str(input["pattern"])
        matches: list = []
        for path in root.rglob(pattern):
            if not any(skip in path.parts for skip in SKIP_DIRS):
                matches.append(path)
                if len(matches) >= MAX_RESULTS:
                    break
        if not matches:
            return ToolResult(f"No files match {pattern!r} under {root}")
        lines = [p.relative_to(root).as_posix() for p in matches]
        truncated = f"\n(truncated at {MAX_RESULTS} results)" if len(lines) >= MAX_RESULTS else ""
        return ToolResult("\n".join(lines) + truncated)
