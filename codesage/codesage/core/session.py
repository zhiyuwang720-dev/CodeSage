"""Session storage: append-only JSONL with fsync (design note #14).

One session = one .jsonl file under the data root. Appends are the only
write path (readers replay the file); a corrupt trailing line is skipped,
never fatal. Single-writer assumption: the CLI is the only process touching
a session (no daemon) — file locking arrives with multi-process needs.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .messages import SessionMessage

_SANITIZE_PROJECT = re.compile(r"[^A-Za-z0-9]+")


class Session:
    def __init__(self, session_id: str, root: Path, project_key: str | None = None):
        self.session_id = session_id
        base = root
        if project_key is not None:
            sanitized = _SANITIZE_PROJECT.sub("-", project_key).strip("-")
            if sanitized:
                base = root / sanitized
        self.path = base / f"{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, message: SessionMessage) -> None:
        """Append one message durably (fsync before returning)."""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(message.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())

    def load(self) -> list[SessionMessage]:
        """Replay the log; corrupt lines are skipped, not fatal."""
        messages: list[SessionMessage] = []
        if not self.path.exists():
            return messages
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = SessionMessage.from_dict(json.loads(line))
                    if message is not None:
                        messages.append(message)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue  # torn/corrupt line: skip, keep the rest
        return messages

    @property
    def exists(self) -> bool:
        return self.path.exists()
