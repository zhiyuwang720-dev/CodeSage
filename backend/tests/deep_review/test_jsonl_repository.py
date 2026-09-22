from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domains.deep_review.storage.jsonl_repository import JsonlDeepReviewStore
from app.domains.deep_review.storage.protocol import DeepReviewStoreError


def test_jsonl_appends_stable_records(tmp_path: Path) -> None:
    store = JsonlDeepReviewStore(tmp_path)
    store.append("run-1", "run_started", {"mode": "preparation"})
    store.append("run-1", "stage_completed", {"stage": "input", "duration_ms": 3})

    lines = (tmp_path / "run-1" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [record["sequence"] for record in records] == [1, 2]
    assert [record["record_type"] for record in records] == ["run_started", "stage_completed"]
    assert all(record["run_id"] == "run-1" for record in records)
    assert all(record["schema_version"] == 1 for record in records)


def test_jsonl_refuses_existing_run_directory(tmp_path: Path) -> None:
    store = JsonlDeepReviewStore(tmp_path)
    store.append("run-1", "run_started", {})
    second = JsonlDeepReviewStore(tmp_path)

    with pytest.raises(DeepReviewStoreError, match="already exists"):
        second.append("run-1", "run_started", {})


def test_jsonl_serialization_failure_does_not_increment(tmp_path: Path) -> None:
    store = JsonlDeepReviewStore(tmp_path)
    store.append("run-1", "run_started", {})

    with pytest.raises(DeepReviewStoreError, match="cannot append"):
        store.append("run-1", "bad_payload", {"value": object()})

    lines = (tmp_path / "run-1" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
