"""Path safety: write-protected paths, symlink-expanded resolution.

Write-protected paths (design note #6): .git/.ssh/settings files etc. can
never be written by the model — these need explicit approval even when an
allow rule matches, and are denied outright under yolo.
"""

from __future__ import annotations

from pathlib import Path

#: Path components that are never writable by tools.
WRITE_PROTECTED_COMPONENTS = frozenset({".git", ".ssh", ".codesage"})

#: Files that are never writable (settings/credentials/agents).
WRITE_PROTECTED_FILENAMES = frozenset(
    {"settings.json", "settings.local.json", "config.json", "agents.md", ".env"}
)

#: Directories whose contents are never writable (config/session state).
WRITE_PROTECTED_DIRS = frozenset({"sessions", "memory", "runs", "worktrees"})


def resolve_candidates(path: Path) -> list[Path]:
    """The real path plus every symlink-expanded candidate (anti-bypass)."""
    try:
        return [path.resolve()]
    except OSError:
        return [path]


def is_write_protected(path: Path) -> bool:
    """True if any resolved candidate touches protected components/files."""
    resolved = path.resolve()
    parts = set(resolved.parts)
    if parts & WRITE_PROTECTED_COMPONENTS:
        return True
    if resolved.name.lower() in WRITE_PROTECTED_FILENAMES:
        return True
    if any(parent.name in WRITE_PROTECTED_DIRS for parent in resolved.parents):
        return True
    return False


def is_sensitive_path(path: Path) -> bool:
    """Reads of sensitive files also need care (keys, history)."""
    return path.name in WRITE_PROTECTED_FILENAMES or ".env" in path.parts
