from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SECRET_KEY = re.compile(r"authorization|api[-_.]?key|access[-_.]?token|secret|password|credential", re.I)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_TOKEN = re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})\b")


def redact_text(value: str, *, max_bytes: int = 8192) -> tuple[str, bool]:
    value = _BEARER.sub(r"\1 [REDACTED]", value)
    value = _TOKEN.sub("[REDACTED]", value)
    try:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.hostname and (parsed.username or parsed.password):
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            value = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        pass
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def sanitize(value: Any, *, key: str = "", max_bytes: int = 8192) -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value, max_bytes=max_bytes)[0]
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize(item, key=str(item_key), max_bytes=max_bytes)
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [sanitize(item, max_bytes=max_bytes) for item in value]
    return value
