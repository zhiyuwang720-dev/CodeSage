"""Atomic file writes: tmp file in the same directory + os.replace.

The tmp+rename pattern is the durability backbone of the whole harness
(settings, sessions, memory — Kode design note #14): readers never see a
half-written file. Kept in config since settings is the first consumer;
later phases import it from here.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path | str, content: str | bytes) -> None:
    """Write *content* to *path* atomically (tmp+rename, fsync).

    Symlinks are resolved first so a linked dotfile (chezmoi/stow) keeps its
    link — the target is replaced, never the link. An existing file's mode is
    preserved (mkstemp tmp files default to 0600). Raises on failure; the
    caller decides whether to degrade.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = None  # new file: keep mkstemp's 0600 default
    data = content.encode("utf-8") if isinstance(content, str) else content
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp_name, path)
        except PermissionError:
            # Windows: os.replace fails with EPERM/EACCES when the target is
            # briefly locked (AV scan, editor); unlink and retry once.
            if path.exists():
                os.unlink(path)
            os.replace(tmp_name, path)
        if mode is not None:
            os.chmod(path, mode)
    except BaseException:
        # Never leave a stray tmp file behind on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json_lossy(path: Path | str, default: dict) -> dict:
    """Read a JSON file; return *default* if missing or corrupt."""
    try:
        import json

        with open(path, encoding="utf-8-sig") as f:  # BOM-tolerant
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default
