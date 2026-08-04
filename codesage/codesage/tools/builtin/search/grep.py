"""Grep tool: regex content search with line numbers."""

from __future__ import annotations

import fnmatch
import re

from ...base import Tool, ToolResult, ToolUseContext
from ._common import MAX_RESULTS, resolve_root, walk_files


class GrepTool(Tool):
    name = "Grep"
    description = "Search file contents with a regex (case-sensitive by default)."
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "Root directory (default: cwd)"},
            "glob": {"type": "string", "description": "Filename filter, e.g. *.py"},
            "-i": {"type": "boolean", "description": "Case-insensitive"},
            "-n": {"type": "boolean", "description": "Show line numbers (default true)"},
        },
        "required": ["pattern"],
    }
    is_concurrency_safe = True

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        root = resolve_root(ctx, input.get("path"))
        try:
            flags = re.IGNORECASE if input.get("-i") else 0
            regex = re.compile(str(input["pattern"]), flags)
        except re.error as exc:
            return ToolResult(f"Error: invalid regex: {exc}", is_error=True)
        glob_filter = str(input.get("glob") or "")
        show_numbers = input.get("-n", True)
        results: list[str] = []
        for path in sorted(walk_files(root)):
            if glob_filter and not fnmatch.fnmatch(path.name, glob_filter):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = path.relative_to(root).as_posix()
                    prefix = f"{rel}:{lineno}: " if show_numbers else f"{rel}: "
                    results.append(prefix + line.strip())
                    if len(results) >= MAX_RESULTS:
                        return ToolResult(
                            "\n".join(results) + f"\n(truncated at {MAX_RESULTS} matches)"
                        )
        return ToolResult("\n".join(results) if results else "No matches")
