"""Search tools: Glob (filenames), Grep (file contents)."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from .base import Tool, ToolError, ToolResult, ToolUseContext

#: Directories never searched (mirrors ripgrep's default ignores).
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".pytest_cache"}

MAX_RESULTS = 100


class GlobTool(Tool):
    name = "Glob"
    description = "Find files by glob pattern under a directory (SKIP_DIRS ignored)."
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. **/*.py"},
            "path": {"type": "string", "description": "Root directory (default: cwd)"},
        },
        "required": ["pattern"],
    }
    is_concurrency_safe = True

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        root = _root(ctx, input.get("path"))
        pattern = str(input["pattern"])
        matches: list[Path] = []
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
        root = _root(ctx, input.get("path"))
        try:
            flags = re.IGNORECASE if input.get("-i") else 0
            regex = re.compile(str(input["pattern"]), flags)
        except re.error as exc:
            return ToolResult(f"Error: invalid regex: {exc}", is_error=True)
        glob_filter = str(input.get("glob") or "")
        show_numbers = input.get("-n", True)
        results: list[str] = []
        for path in sorted(_walk_files(root)):
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


def _root(ctx: ToolUseContext, path: str | None) -> Path:
    p = Path(path) if path else ctx.cwd
    if not p.is_absolute():
        p = ctx.cwd / p
    return p.resolve()


def _walk_files(root: Path):
    """Yield text-ish files under root, skipping SKIP_DIRS."""
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        dir_path = stack.pop()
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                yield entry
