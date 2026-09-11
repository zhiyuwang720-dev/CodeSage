"""A26-A28: offline bundle, deterministic summary, and public redaction."""

from __future__ import annotations

import json
from pathlib import Path

from app.diagnostics.__main__ import main
from app.diagnostics.exporter import export_bundle, summarize_bundle


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "process_instance": "p1",
            "process_sequence": 1,
            "event_name": "model.request",
            "message": "api_key=secret",
            "review_run_id": "run-1",
            "session_id": "s1",
            "turn_id": "t1",
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "process_instance": "p1",
            "process_sequence": 2,
            "event_name": "tool.completed",
            "message": "Read",
            "review_run_id": "run-1",
            "session_id": "s1",
            "turn_id": "t1",
        },
    ]
    (logs / "api.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    content = tmp_path / "content"
    run_dir = content / "run-1" / "model_request"
    run_dir.mkdir(parents=True)
    (run_dir / "artifact.json").write_text('{"safe":"visible","password":"secret"}', encoding="utf-8")
    return logs, content


def test_a26_export_and_summarize_are_offline_and_deterministic(tmp_path: Path, acceptance_artifact_root) -> None:
    logs, content = _sources(tmp_path)
    bundle, code = export_bundle(run_id="run-1", output=tmp_path / "bundle", logs_root=logs, content_root=content)
    assert code == 2
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "timeline.jsonl").is_file()
    assert (bundle / "logs.jsonl").is_file()
    assert (bundle / "model-calls.jsonl").is_file()
    assert (bundle / "tool-calls.jsonl").is_file()
    assert (bundle / "findings-decisions.jsonl").is_file()
    assert (bundle / "completeness.json").is_file()
    assert (bundle / "report.html").is_file()
    assert list((bundle / "contents").rglob("*.json"))
    first = summarize_bundle(bundle)
    second = summarize_bundle(bundle)
    assert first == second
    assert first["files"]["logs.jsonl"] == 2
    assert first["files"]["model-calls.jsonl"] == 1
    assert first["files"]["tool-calls.jsonl"] == 1

    evidence = acceptance_artifact_root / "evidence" / "a27" / "summary.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(first, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def test_a28_public_bundle_omits_contents_and_internal_fields(tmp_path: Path) -> None:
    logs, content = _sources(tmp_path)
    bundle, _ = export_bundle(
        run_id="run-1",
        output=tmp_path / "public-bundle",
        public=True,
        logs_root=logs,
        content_root=content,
    )
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    public_logs = (bundle / "logs.jsonl").read_text(encoding="utf-8")
    assert manifest["sources"]["logs_root"] is None
    assert manifest["sources"]["content_root"] is None
    assert not (bundle / "contents").exists()
    assert str(logs) not in public_logs
    assert "secret" not in public_logs


def test_a28_cli_summarize_does_not_require_network(tmp_path: Path, capsys) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("timeline.jsonl", "logs.jsonl", "model-calls.jsonl", "tool-calls.jsonl", "findings-decisions.jsonl"):
        (bundle / name).write_text("", encoding="utf-8")
    (bundle / "completeness.json").write_text("{}", encoding="utf-8")
    assert main(["summarize", "--input", str(bundle)]) == 0
    assert "completeness" in capsys.readouterr().out
