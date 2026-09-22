from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import DeepReviewStoreError


class JsonlDeepReviewStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._sequences: dict[str, int] = {}

    def append(self, run_id: str, record_type: str, payload: dict[str, Any]) -> None:
        if not run_id or Path(run_id).name != run_id:
            raise DeepReviewStoreError("run_id must be a safe directory name")
        if not record_type:
            raise DeepReviewStoreError("record_type is required")

        run_dir = self.root / run_id
        if run_id not in self._sequences:
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise DeepReviewStoreError(f"run directory already exists: {run_id}") from exc
            except OSError as exc:
                raise DeepReviewStoreError(f"cannot create run directory: {exc}") from exc
            self._sequences[run_id] = 0

        record = {
            "schema_version": 1,
            "sequence": self._sequences[run_id] + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "record_type": record_type,
            "payload": payload,
        }
        try:
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ) + "\n"
            path = run_dir / "events.jsonl"
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError) as exc:
            raise DeepReviewStoreError(f"cannot append run event: {exc}") from exc
        self._sequences[run_id] += 1


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"
