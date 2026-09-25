from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domains.deep_review.storage.jsonl_repository import JsonlDeepReviewStore, read_complete_events
from app.domains.deep_review.storage.protocol import DeepReviewStoreError


def test_final_and_process_reports_are_materialized_from_same_run(tmp_path: Path) -> None:
    store = JsonlDeepReviewStore(tmp_path)
    store.append("run-1", "run_started", {})
    store.append("run-1", "final_result", {"result": {"run_id": "run-1", "status": "completed"}})
    store.append("run-1", "process_report", {"report": {"run_id": "run-1", "mode": "final"}})
    run_dir = tmp_path / "run-1"
    assert json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["status"] == "completed"
    assert json.loads((run_dir / "process_report.json").read_text(encoding="utf-8"))["mode"] == "final"
    records = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["sequence"] for item in records] == [1, 2, 3]


def test_report_replace_failure_leaves_diagnostic_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.domains.deep_review.storage import jsonl_repository

    store = JsonlDeepReviewStore(tmp_path)
    store.append("run-1", "run_started", {})

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(jsonl_repository.os, "replace", fail_replace)
    with pytest.raises(DeepReviewStoreError, match="injected replace failure"):
        store.append("run-1", "process_report", {"report": {"run_id": "run-1"}})
    assert not (tmp_path / "run-1" / "process_report.json").exists()
    assert [json.loads(line)["record_type"] for line in (tmp_path / "run-1" / "events.jsonl").read_text(encoding="utf-8").splitlines()] == ["run_started", "process_report"]


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


def test_diagnostic_reader_stops_before_truncated_tail(tmp_path: Path) -> None:
    store = JsonlDeepReviewStore(tmp_path)
    store.append("run-1", "run_started", {})
    path = tmp_path / "run-1" / "events.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"schema_version":1,"sequence":2')
    events = read_complete_events(path)
    assert len(events) == 1 and events[0]["record_type"] == "run_started"
