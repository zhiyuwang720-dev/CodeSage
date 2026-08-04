"""Shared filesystem-tool helpers (path resolution, binary sniffing, decoding)."""

from __future__ import annotations

from pathlib import Path

from ...base import ToolUseContext

_BINARY_SNIFF_BYTES = 8192
_SAFE_ENCODINGS = ("utf-8", "gb18030", "latin-1")


def resolve_path(ctx: ToolUseContext, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ctx.cwd / p
    return p.resolve()


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
