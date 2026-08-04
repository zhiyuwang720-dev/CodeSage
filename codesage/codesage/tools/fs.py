"""Filesystem tools: LS, Read, Write, Edit."""

from __future__ import annotations

from pathlib import Path

from ..config import atomic_write
from .base import Tool, ToolError, ToolResult, ToolUseContext

DEFAULT_READ_LINES = 2000
MAX_READ_LINES = 20000
_BINARY_SNIFF_BYTES = 8192
_SAFE_ENCODINGS = ("utf-8", "gb18030", "latin-1")


def _resolve_path(ctx: ToolUseContext, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ctx.cwd / p
    return p.resolve()


def _is_binary(path: Path) -> bool:
    """Sniff the first bytes for NULs (classic binary heuristic)."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return False


def _decode(data: bytes) -> str:
    for encoding in _SAFE_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class LSTool(Tool):
    name = "LS"
    description = "List directory contents (names and types only)."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory to list (default: cwd)"}},
    }
    is_concurrency_safe = True

    async def _run(self, input: dict, ctx: ToolUseContext) -> ToolResult:
        path = _resolve_path(ctx, str(input.get("path") or "."))
        if not path.is_dir():
            return ToolResult(f"Error: {path} is not a directory", is_error=True)
        entries = []
        for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            kind = "dir" if entry.is_dir() else "file"
            entries.append(f"{entry.name}/" if kind == "dir" else entry.name)
        return ToolResult("\n".join(entries) if entries else "(empty)")


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
        path = _resolve_path(ctx, str(input["file_path"]))
        if not path.is_file():
            return ToolResult(f"Error: {path} does not exist or is not a file", is_error=True)
        if _is_binary(path):
            return ToolResult(f"Error: {path} appears to be a binary file; Read only handles text", is_error=True)
        offset = int(input.get("offset") or 0)
        limit = int(input.get("limit") or DEFAULT_READ_LINES)
        limit = min(max(limit, 1), MAX_READ_LINES)
        try:
            data = path.read_bytes()
        except OSError as exc:
            return ToolResult(f"Error reading {path}: {exc}", is_error=True)
        text = _decode(data)
        lines = text.splitlines()
        if offset >= len(lines):
            return ToolResult(f"Error: offset {offset} beyond file length {len(lines)}", is_error=True)
        window = lines[offset : offset + limit]
        numbered = "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(window))
        truncated = len(lines) > offset + limit
        suffix = f"\n(truncated: showing lines {offset + 1}-{offset + len(window)} of {len(lines)})" if truncated else ""
        return ToolResult(numbered + suffix)


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
        path = _resolve_path(ctx, str(input["file_path"]))
        content = str(input["content"])
        try:
            atomic_write(path, content)
        except OSError as exc:
            return ToolResult(f"Error writing {path}: {exc}", is_error=True)
        return ToolResult(f"Wrote {len(content)} bytes to {path}")


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
        path = _resolve_path(ctx, str(input["file_path"]))
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
