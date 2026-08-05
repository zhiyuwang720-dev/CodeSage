"""Path safety: write-protected paths, symlink-expanded resolution.

Write-protected paths (design note #6): .git/.ssh/settings files etc. can
never be written by the model — these need explicit approval even when an
allow rule matches, and are denied outright under yolo.
"""

from __future__ import annotations

from pathlib import Path

#: Path components that are never writable by tools.
WRITE_PROTECTED_COMPONENTS = frozenset({".git", ".ssh", ".codesage"})

#: Files that are never writable (settings/credentials/agents/shell config).
WRITE_PROTECTED_FILENAMES = frozenset(
    {
        "settings.json", "settings.local.json", "config.json", "agents.md", ".env",
        ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc",
        ".zprofile", ".profile", ".mcp.json",
    }
)

#: Directories whose contents are never writable (config/session state/IDE).
WRITE_PROTECTED_DIRS = frozenset({"sessions", "memory", "runs", "worktrees", ".vscode", ".idea"})

#: Windows reserved device names (with any extension).
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)


def resolve_candidates(path: Path) -> list[Path]:
    """The real path plus every symlink-expanded candidate (anti-bypass)."""
    try:
        return [path.resolve()]
    except OSError:
        return [path]


def is_write_protected(path: Path) -> bool:
    """True if any resolved candidate touches protected components/files."""
    raw = str(path)
    if raw.startswith("\\\\") or raw.startswith("//"):
        return True  # UNC share or \\?\ extended-length prefix
    if ".." in path.parts:
        return True  # traversal segment — refuse outright
    try:
        resolved = path.resolve()
    except OSError:
        return True  # unresolvable (e.g. dead UNC) — conservative
    parts = set(resolved.parts)
    if parts & WRITE_PROTECTED_COMPONENTS:
        return True
    name = resolved.name.lower()
    if name in WRITE_PROTECTED_FILENAMES or resolved.stem.lower() in _RESERVED_NAMES:
        return True
    if any(parent.name in WRITE_PROTECTED_DIRS for parent in resolved.parents):
        return True
    return False


def is_sensitive_path(path: Path) -> bool:
    """Reads of sensitive files also need care (keys, history)."""
    return path.name in WRITE_PROTECTED_FILENAMES or ".env" in path.parts
