"""A10/C02: the real LiteLLM SDK path captures model request and response content."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.infrastructure.observability.content import CAPTURE_STATUS_CAPTURED


def test_a10_litellm_model_response_content_is_captured(acceptance_artifact_root) -> None:
    root = acceptance_artifact_root / "evidence" / "a10"
    output = root / "live_capture"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.plan20_acceptance.a10_capture_probe",
            "--output",
            str(output),
            "--run-id",
            "a10-live-capture",
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    record = json.loads(completed.stdout.strip().splitlines()[-1])
    assert record["span_name"] == "litellm_request"
    assert record["model_request_capture_status"] == CAPTURE_STATUS_CAPTURED
    assert record["model_response_capture_status"] == CAPTURE_STATUS_CAPTURED
    assert record["model_response_reason"] is None
    assert record["response_artifact_verified_bytes"] and record["response_artifact_verified_bytes"] > 0
    assert "capture-ok" in record["output_value"]
    (root / "live_capture.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )