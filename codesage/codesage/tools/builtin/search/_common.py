"""Shared search-tool helpers: ignored dirs, root resolution, file walking."""

from __future__ import annotations

from pathlib import Path

from ...base import ToolUseContext

#: Directories never searched (mirrors ripgrep's default ignores).
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".pytest_cache"}

MAX_RESULTS = 100


def resolve_root(ctx: ToolUseContext, path: str | None) -> Path:
    p = Path(path) if path else ctx.cwd
    if not p.is_absolute():
        p = ctx.cwd / p
    return p.resolve()


def walk_files(root: Path):
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
