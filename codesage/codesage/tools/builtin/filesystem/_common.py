"""Shared filesystem-tool helpers (path resolution, binary sniffing, decoding,
read-freshness bookkeeping for the Edit/Write stale-file guard)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ...base import ToolUseContext

_BINARY_SNIFF_BYTES = 8192
_SAFE_ENCODINGS = ("utf-8", "gb18030", "latin-1")


def resolve_path(ctx: ToolUseContext, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ctx.cwd / p
    return p.resolve()


def record_read(ctx: ToolUseContext, path: Path, data: bytes) -> None:
    """Remember path's mtime+hash so Edit/Write can detect off-band changes."""
    try:
        ctx.read_file_timestamps[path] = path.stat().st_mtime_ns
    except OSError:
        return
    ctx.read_file_hashes[path] = hashlib.sha256(data).hexdigest()


def record_written(ctx: ToolUseContext, path: Path, content: str) -> None:
    """After our own successful write: update the read-state to what we wrote."""
    data = content.encode("utf-8")
    ctx.read_file_timestamps[path] = path.stat().st_mtime_ns
    ctx.read_file_hashes[path] = hashlib.sha256(data).hexdigest()


def ensure_read_freshness(ctx: ToolUseContext, path: Path) -> str | None:
    """Guard against silent clobbering of externally changed files.

    Only meaningful for paths previously Read (checked by the caller).
    Returns None when fresh, or the error string for the tool result.
    A pure mtime move (content identical — e.g. a touch) just refreshes.

    The hash is the only reliable freshness signal: on filesystems where
    two writes can land on the same mtime tick, an mtime match alone would
    miss an external content change (observed flaky on Windows).
    """
    try:
        mtime_ns = path.stat().st_mtime_ns
        data = path.read_bytes()
    except OSError as exc:
        return f"Error reading {path}: {exc}"
    digest = hashlib.sha256(data).hexdigest()
    if digest == ctx.read_file_hashes.get(path):
        if mtime_ns != ctx.read_file_timestamps.get(path):
            ctx.read_file_timestamps[path] = mtime_ns  # touched, not changed
        return None
    return "File changed since Read; re-Read it"


def is_binary(path: Path) -> bool:
    """Sniff the first bytes for NULs (classic binary heuristic)."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return False


def decode_text(data: bytes) -> str:
    for encoding in _SAFE_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
