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
    """Write *content* to *path* atomically (tmp+rename, fsync)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
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

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default
