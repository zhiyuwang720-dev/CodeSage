"""A23-A25: JSON logging, rotation, and unified bootstrap wiring."""

from __future__ import annotations

import json
from pathlib import Path

from opentelemetry import trace

from app.infrastructure.observability.logging import configure_logging
from app.infrastructure.observability.tracing import bind_observability_context, reset_observability_context


def test_a23_json_log_contains_trace_and_business_fields(tmp_path: Path, acceptance_artifact_root) -> None:
    log_root = tmp_path / "logs"
    logger = configure_logging(service_name="a23-service", log_root=log_root, console=False)
    token = bind_observability_context(task_id="task-1", review_run_id="run-1", session_id="session-1")
    try:
        with trace.get_tracer("test").start_as_current_span("unit.span"):
            logger.info("hello", extra={"event_name": "stage.started"})
    finally:
        reset_observability_context(token)
    files = list(log_root.glob("*.jsonl"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    assert payload["schema_version"] == 1
    assert payload["event_name"] == "stage.started"
    assert payload["message"] == "hello"
    assert payload["task_id"] == "task-1"
    assert payload["review_run_id"] == "run-1"
    assert payload["session_id"] == "session-1"
    assert payload["trace_id"] and payload["span_id"]
    assert payload["process_instance"]
    assert payload["process_sequence"] >= 1

    target = acceptance_artifact_root / "evidence" / "a23" / "log.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def test_a24_file_rotation_creates_backups(tmp_path: Path) -> None:
    log_root = tmp_path / "rotating"
    logger = configure_logging(
        service_name="a24-service", log_root=log_root, max_bytes=256, backup_count=2, console=False
    )
    for index in range(50):
        logger.info("payload-%s-%s", index, "x" * 40)
    files = list(log_root.glob("a24-service-*.jsonl*"))
    assert len(files) >= 2


def test_a25_local_exporter_is_batch_and_worker_bootstrap_is_shared() -> None:
    setup_source = Path("app/infrastructure/observability/setup.py").read_text(encoding="utf-8")
    worker_source = Path("app/nodes/pr_review/worker.py").read_text(encoding="utf-8")
    assert "SimpleSpanProcessor(" not in setup_source
    assert "max_queue_size=2048" in setup_source
    assert "schedule_delay_millis=1000" in setup_source
    assert "on_startup=WorkerSettings.on_startup" in worker_source
    assert "on_shutdown=WorkerSettings.on_shutdown" in worker_source
    assert "configure_logging(service_name=" in worker_source
