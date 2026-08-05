"""Path safety: write-protected paths, symlink-expanded resolution.

Write-protected paths (design note #6): .git/.ssh/settings files etc. can
never be written by the model — these need explicit approval even when an
allow rule matches, and are denied outright under yolo.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Path components that are never writable by tools. @SSL@ / DavWWWRoot are
#: the IIS virtual routes that escape a share's real directory tree.
WRITE_PROTECTED_COMPONENTS = frozenset({".git", ".ssh", ".codesage", "@SSL@", "DavWWWRoot"})

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

#: IP-address UNC share (\\x.x.x.x\share) — a raw-IP network target.
_IP_UNC_RE = re.compile(r"^\\\\\d{1,3}(?:\.\d{1,3}){3}\\")


def resolve_candidates(path: Path) -> list[Path]:
    """The real path plus every symlink-expanded candidate (anti-bypass)."""
    try:
        return [path.resolve()]
    except OSError:
        return [path]


def is_write_protected(path: Path) -> bool:
    """True if any resolved candidate touches protected components/files."""
    raw = str(path)
    if raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")) or _IP_UNC_RE.match(raw):
        return True  # UNC / extended-length / device prefixes — refuse outright
    if raw.find(":", 2) != -1:
        return True  # NTFS alternate data stream (drive colon sits at index 1)
    if ".." in path.parts:
        return True  # traversal segment — refuse outright
    try:
        resolved = path.resolve()
    except OSError:
        return True  # unresolvable (e.g. dead UNC) — conservative
    parts = {part.rstrip(" .") for part in resolved.parts}  # Windows strips trailing dots/spaces
    if parts & WRITE_PROTECTED_COMPONENTS:
        return True
    name = resolved.name.lower().rstrip(" .")
    if name in WRITE_PROTECTED_FILENAMES or Path(name).stem.lower() in _RESERVED_NAMES:
        return True
    if any(parent.name.rstrip(" .") in WRITE_PROTECTED_DIRS for parent in resolved.parents):
        return True
    return False


def is_sensitive_path(path: Path) -> bool:
    """Reads of sensitive files also need care (keys, history)."""
    return path.name in WRITE_PROTECTED_FILENAMES or ".env" in path.parts
