"""AGENTS.md discovery: git root → cwd, override files take precedence.

Path discovery only (design note #19); content reading/injection lands in
the context phase (08). Pure filesystem walk — no git subprocess, so it
works in any checkout and is trivially testable.
"""

from __future__ import annotations

from pathlib import Path

AGENTS_FILENAME = "AGENTS.md"
AGENTS_OVERRIDE_FILENAME = "AGENTS.override.md"


def find_git_root(start: Path) -> Path | None:
    """Walk upward from *start* for a .git dir/file; return repo root or None."""
    for parent in [start, *start.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def get_project_instruction_files(cwd: Path | None = None) -> list[Path]:
    """Ordered instruction files from git root down to cwd (outermost first).

    At each level, AGENTS.override.md replaces AGENTS.md. Without a git root,
    falls back to cwd alone (Kode's ``gitRoot ?? resolve(cwd)``): at least
    the cwd-level AGENTS.md/AGENTS.override.md is still read.
    """
    start = (cwd or Path.cwd()).resolve()
    git_root = find_git_root(start) or start

    # Walk start up to git_root (a guaranteed ancestor), then back down
    # outer→inner. Counting upward first also avoids the Windows quirk where
    # Path("C:\\").parent is itself (an upward loop would never terminate).
    levels: list[Path] = []
    level = start
    while True:
        levels.append(level)
        if level == git_root:
            break
        level = level.parent

    files: list[Path] = []
    for level in reversed(levels):
        override = level / AGENTS_OVERRIDE_FILENAME
        if override.is_file():
            files.append(override)
        else:
            plain = level / AGENTS_FILENAME
            if plain.is_file():
                files.append(plain)
        if level == start:
            break
        level = level.parent
    return files
