"""Audit trail: every permission decision emits one event.

The audit hook ships from day one (project intent: security-domain
adaptation consumes these events without touching the engine). The sink is
replaceable: default writes to a session JSONL; phase 16+ may add sandbox
or threat-model consumers.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class ToolAuditEvent:
    """One permission decision, immutable."""

    tool_name: str
    decision: str  # allow | ask | deny
    reason: str | None = None
    source: str | None = None  # rule matched / mode / write-protection / ...
    mode: str = "default"
    input_summary: dict[str, Any] | None = None
    timestamp: str = ""


class AuditSink(Protocol):
    def emit(self, event: ToolAuditEvent) -> None: ...


class JsonlAuditSink:
    """Append-only audit log (one event per line)."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: ToolAuditEvent) -> None:
        data = asdict(event)
        if not data["timestamp"]:
            data["timestamp"] = datetime.now(timezone.utc).isoformat()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class NullAuditSink:
    """No-op sink (tests, headless)."""

    def emit(self, event: ToolAuditEvent) -> None:
        pass
