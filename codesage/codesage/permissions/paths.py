"""Path safety: write-protected paths, symlink-expanded resolution.

Write-protected paths (design note #6): .git/.ssh/settings files etc. can
never be written by the model — these need explicit approval even when an
allow rule matches, and are denied outright under yolo.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Path components that are never writable by tools. @SSL@ / DavWWWRoot are
#: the IIS virtual routes that escape a share's real directory tree. All
#: comparisons are case-insensitive (Windows/macOS; POSIX lowercase is a
#: conservative no-op), so .GIT / .SSH spellings are protected too.
WRITE_PROTECTED_COMPONENTS = frozenset({".git", ".ssh", ".codesage", "@ssl@", "davwwwroot"})

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
    """The lexical absolute path plus the symlink-expanded real path
    (anti-bypass: a deny/allow rule on the unexpanded spelling, e.g.
    /tmp/link/** when /tmp/link → ~/.ssh, must still hit). Deduplicated."""
    try:
        real = path.resolve()
    except OSError:
        real = path
    out: list[Path] = []
    for cand in (path, real):
        if cand not in out:
            out.append(cand)
    return out


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
    parts = {part.lower().rstrip(" .") for part in resolved.parts}  # case-insensitive; Windows strips trailing dots/spaces
    if parts & WRITE_PROTECTED_COMPONENTS:
        return True
    name = resolved.name.lower().rstrip(" .")
    if name in WRITE_PROTECTED_FILENAMES or Path(name).stem.lower() in _RESERVED_NAMES:
        return True
    if any(parent.name.lower().rstrip(" .") in WRITE_PROTECTED_DIRS for parent in resolved.parents):
        return True
    return False


def is_sensitive_path(path: Path) -> bool:
    """Reads of sensitive files also need care (keys, history)."""
    return path.name.lower() in WRITE_PROTECTED_FILENAMES or any(p.lower() == ".env" for p in path.parts)
