"""A10-A13/A19: bounded content, redaction, download primitives, provenance."""

from __future__ import annotations

import json
from pathlib import Path

from app.domains.pr_review.synthesizer import synthesize
from app.infrastructure.observability.content import (
    CAPTURE_STATUS_CAPTURED,
    CAPTURE_STATUS_DISABLED,
    CAPTURE_STATUS_TRUNCATED,
    DiagnosticContentStore,
)
from app.infrastructure.observability.privacy import redact_text


def test_a10_capture_enabled_redacts_and_persists_verified_content(tmp_path: Path) -> None:
    store = DiagnosticContentStore(
        tmp_path,
        enabled=True,
        preview_bytes=48,
        file_limit_bytes=4096,
        run_limit_bytes=8192,
    )
    result = store.capture(
        run_id="run-a10",
        kind="tool_result",
        content={
            "authorization": "Bearer top-secret-token",
            "url": "https://user:password@example.test/path?token=query-secret",
            "nested": {"password": "p@ss", "safe": "visible"},
        },
    )

    assert result.status == CAPTURE_STATUS_CAPTURED
    assert result.artifact is not None
    assert result.preview is not None
    assert len(result.preview.encode("utf-8")) <= 48
    raw = store.read_verified(result.artifact)
    text = raw.decode("utf-8")
    assert "top-secret-token" not in text
    assert "password@example.test" not in text
    assert "query-secret" not in text
    assert "p@ss" not in text
    assert "visible" in text
    assert store.find_artifact("run-a10", result.artifact.artifact_id) == result.artifact

    target = tmp_path / "a10.json"
    target.write_text(json.dumps({"status": result.status, "path": result.artifact.relative_path}), encoding="utf-8")


def test_a10_capture_disabled_has_metadata_but_no_body(tmp_path: Path) -> None:
    store = DiagnosticContentStore(tmp_path, enabled=False)
    result = store.capture(run_id="run-off", kind="model_request", content={"prompt": "private"})
    assert result.status == CAPTURE_STATUS_DISABLED
    assert result.artifact is None
    assert result.preview is None
    assert result.original_redacted_bytes > 0


def test_a11_file_limit_and_run_quota_mark_truncated(tmp_path: Path) -> None:
    store = DiagnosticContentStore(
        tmp_path,
        enabled=True,
        preview_bytes=16,
        file_limit_bytes=80,
        run_limit_bytes=100,
    )
    first = store.capture(run_id="run-limit", kind="first", content="x" * 80)
    second = store.capture(run_id="run-limit", kind="second", content="y" * 80)
    assert first.status == CAPTURE_STATUS_CAPTURED
    assert first.stored_bytes == 80
    assert second.status == CAPTURE_STATUS_TRUNCATED
    assert second.reason == "run_quota"
    assert second.stored_bytes == 20
    assert second.artifact is not None
    assert len(store.read_verified(second.artifact)) == 20


def test_a12_redaction_covers_json_strings_urls_and_embedded_secrets() -> None:
    text, truncated = redact_text(
        '{"api_key":"sk-abcdefghijklmnop","url":"https://u:p@example.test/x?access_token=abc",'
        '"note":"Authorization: Bearer abcdefghijklmnop"}'
    )
    assert truncated is False
    assert "sk-abcdefghijklmnop" not in text
    assert "u:p@" not in text
    assert "abcdefghijklmnop" not in text


def test_a19_finding_provenance_records_ids_and_reasons() -> None:
    finding = {
        "rule_id": "security-R",
        "severity": "high",
        "category": "security",
        "title": "duplicate issue",
        "description": "d",
        "file_path": "a.py",
        "line_start": 3,
        "line_end": 3,
        "confidence": 0.8,
        "needs_verification": False,
        "verdict": "confirmed",
        "source": "security",
    }
    duplicate = dict(finding, source="architecture", confidence=0.9)
    result = synthesize([finding, duplicate], min_severity="high")
    provenance = result.provenance
    assert provenance["rule_version"] == "pr_review_synthesizer_v1"
    assert len(provenance["input_finding_ids"]) == 2
    assert len(provenance["output_finding_ids"]) == 1
    assert provenance["deduped_away"] == 1
