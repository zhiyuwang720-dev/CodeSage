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
        report_name = {"final_result": "result.json", "process_report": "process_report.json"}.get(record_type)
        report_payload = payload.get("result" if record_type == "final_result" else "report") if report_name else None
        temporary_path: Path | None = None
        try:
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ) + "\n"
            if report_name:
                if not isinstance(report_payload, dict):
                    raise ValueError(f"{record_type} requires a complete report object")
                report_encoded = json.dumps(report_payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                temporary_path = run_dir / f".{report_name}.{uuid.uuid4().hex}.tmp"
                with temporary_path.open("x", encoding="utf-8", newline="\n") as report_handle:
                    report_handle.write(report_encoded)
                    report_handle.flush()
                    os.fsync(report_handle.fileno())
            path = run_dir / "events.jsonl"
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._sequences[run_id] += 1
            if report_name and temporary_path is not None:
                os.replace(temporary_path, run_dir / report_name)
        except (OSError, TypeError, ValueError) as exc:
            raise DeepReviewStoreError(f"cannot append run event: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"


def read_complete_events(path: str | Path) -> list[dict[str, Any]]:
    """Read durable full lines for diagnostics only; never resume a run from them."""
    events: list[dict[str, Any]] = []
    with Path(path).open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                break
            record = json.loads(line)
            if not isinstance(record, dict):
                raise DeepReviewStoreError("run event must be a JSON object")
            events.append(record)
    return events
