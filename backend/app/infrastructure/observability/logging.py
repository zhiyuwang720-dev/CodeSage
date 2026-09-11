"""Unified JSON logging with process-local rotation and request correlation."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

from opentelemetry import trace

from app.infrastructure.observability.tracing import get_observability_context

LOG_SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
_process_instance = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_sequence_lock = threading.Lock()
_sequence = 0


def _next_sequence() -> int:
    global _sequence
    with _sequence_lock:
        _sequence += 1
        return _sequence


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "schema_version": LOG_SCHEMA_VERSION,
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "event_name": getattr(record, "event_name", record.name),
            "message": record.getMessage(),
            "service": self.service_name,
            "process_instance": _process_instance,
            "trace_id": format(span_context.trace_id, "032x") if span_context.is_valid else None,
            "span_id": format(span_context.span_id, "016x") if span_context.is_valid else None,
            "process_sequence": _next_sequence(),
        }
        payload.update(get_observability_context())
        if getattr(record, "error_kind", None):
            payload["error_kind"] = getattr(record, "error_kind")
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if getattr(record, "artifact_refs", None):
            payload["artifact_refs"] = list(record.artifact_refs)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    *,
    service_name: str,
    log_root: str | Path = ".codesage/observability/logs",
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    console: bool = True,
) -> logging.Logger:
    """Configure root logging once per process and return the root logger."""

    root = logging.getLogger()
    configured_name = getattr(root, "_codesage_json_logging", None)
    if configured_name == service_name:
        return root
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.INFO)
    formatter = JsonFormatter(service_name=service_name)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    root_path = Path(log_root)
    root_path.mkdir(parents=True, exist_ok=True)
    file_path = root_path / f"{service_name}-{_process_instance.replace(':', '-')}.jsonl"
    rotating = RotatingFileHandler(file_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    rotating.setFormatter(formatter)
    root.addHandler(rotating)
    setattr(root, "_codesage_json_logging", service_name)
    return root


def flush_logs(timeout_seconds: float = 2.0) -> bool:
    del timeout_seconds
    result = True
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            result = False
    return result


def log_event(logger: logging.Logger, event_name: str, message: str, **fields: Any) -> None:
    logger.info(message, extra={"event_name": event_name, **fields})


__all__ = [
    "JsonFormatter",
    "LOG_SCHEMA_VERSION",
    "configure_logging",
    "flush_logs",
    "log_event",
]
