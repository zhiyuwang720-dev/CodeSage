from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def write_jsonl_atomic(path: str | Path, records: Iterable[BaseModel | dict]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                payload = record.model_dump(mode="json") if isinstance(record, BaseModel) else dict(record)
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def read_jsonl(path: str | Path, model: type[T] | None = None) -> list[T | dict]:
    records = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"blank JSONL line at {line_number}")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL line {line_number}: {exc.msg}") from exc
        records.append(model.model_validate(payload) if model else payload)
    return records


def upsert_records(path: str | Path, records: Iterable[BaseModel], *, key: str) -> None:
    destination = Path(path)
    merged: dict[str, BaseModel | dict] = {}
    if destination.exists():
        for existing in read_jsonl(destination):
            merged[str(existing[key])] = existing
    for record in records:
        payload = record.model_dump(mode="json")
        merged[str(payload[key])] = record
    write_jsonl_atomic(destination, (merged[item] for item in sorted(merged)))
