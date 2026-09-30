from __future__ import annotations

import json
import subprocess
from pathlib import Path

import config
import evaluate
import pipeline
from reviewers import codesage_deep
from schema import ReviewInstance


def _instance() -> ReviewInstance:
    return ReviewInstance(
        instance_id="owner__repo@1234567", repo="owner/repo",
        base_commit="a" * 40, head_commit="b" * 40,
    )


def _final_payload(*, findings: list[dict] | None = None) -> dict:
    return {
        "run_id": "run-123", "status": "completed", "findings": findings or [],
        "summary": "review complete", "unresolved_risks": [],
        "metrics": {
            "reviewed_files": 1, "model_calls": 4, "duration_ms": 100,
            "input_tokens": 123, "output_tokens": 45,
        },
        "content_hash": "a" * 64, "candidate_count": len(findings or []),
        "cross_status": "completed", "diagnostics": [],
    }


def test_adapter_calls_same_cli_and_evaluator_reads_one_comment(tmp_path: Path, monkeypatch) -> None:
    instance = _instance()
    monkeypatch.setattr(codesage_deep, "prepare_repo", lambda **_kwargs: tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["cwd"] == codesage_deep.DEFAULT_BACKEND_ROOT.resolve()
        assert "--through" in command and command[command.index("--through") + 1] == "final"
        assert "--allow-model-calls" in command
        raw_path = Path(command[command.index("--output") + 1])
        domain_dir = Path(command[command.index("--store-dir") + 1])
        raw_path.write_text(json.dumps(_final_payload(findings=[{
            "file_path": "src/a.py", "line_start": 5, "line_end": 6,
            "severity": "low", "title": "Typo", "body": "A changed error text is misspelled.",
            "evidence": "src/a.py:5", "suggestion": "Correct the text.",
        }])), encoding="utf-8")
        process_path = domain_dir / "run-123" / "process_report.json"
        process_path.parent.mkdir(parents=True)
        process_path.write_text(json.dumps({
            "run_id": "run-123", "final_status": "completed", "final_content_hash": "a" * 64,
        }), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(codesage_deep.subprocess, "run", fake_run)
    results_dir = tmp_path / "results"
    summary = codesage_deep.review_instance(
        instance, tmp_path / "repo", results_dir,
        allow_model_calls=True, python_executable="python-test",
    )
    assert summary["status"] == "ok" and len(calls) == 1
    comments = evaluate.load_target_comments(results_dir, "codesage_deep", instance.instance_id)
    assert comments is not None and len(comments) == 1
    assert comments[0]["path"] == "src/a.py"
    assert comments[0]["from_line"] == 5 and comments[0]["to_line"] == 6
    assert "Typo" in comments[0]["note"]
    envelope = json.loads(config.result_path(results_dir, instance.instance_id).read_text(encoding="utf-8"))
    assert envelope["review"]["summary"] == "review complete"
    usage = evaluate._extract_usage_from_result(results_dir, "codesage_deep", instance.instance_id)
    assert usage == {
        "duration_seconds": envelope["duration_seconds"],
        "input_tokens": 123,
        "output_tokens": 45,
    }


def test_codesage_usage_fix_preserves_ocr_summary_shape(tmp_path: Path) -> None:
    instance = _instance()
    path = config.result_path(tmp_path, instance.instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "duration_seconds": 12.5,
        "review": {"summary": {"input_tokens": 78, "output_tokens": 9}},
    }), encoding="utf-8")

    assert evaluate._extract_usage_from_result(tmp_path, "ocr", instance.instance_id) == {
        "duration_seconds": 12.5,
        "input_tokens": 78,
        "output_tokens": 9,
    }


def test_adapter_rejects_preparation_without_success_envelope(tmp_path: Path, monkeypatch) -> None:
    instance = _instance()
    monkeypatch.setattr(codesage_deep, "prepare_repo", lambda **_kwargs: tmp_path)

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        Path(command[command.index("--output") + 1]).write_text(
            json.dumps({"run_id": "run-123", "mode": "preparation", "pipeline_complete": False}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(codesage_deep.subprocess, "run", fake_run)
    results_dir = tmp_path / "results"
    summary = codesage_deep.review_instance(
        instance, tmp_path / "repo", results_dir,
        allow_model_calls=True,
    )
    assert summary["status"] == "failed"
    assert not config.result_path(results_dir, instance.instance_id).exists()


def test_pipeline_dispatches_codesage_without_other_reviewers(monkeypatch, tmp_path: Path) -> None:
    instance = _instance()
    calls: list[str] = []
    monkeypatch.setattr(codesage_deep, "review_instance", lambda **_kwargs: calls.append("deep") or {"status": "preview"})
    result = pipeline._review_one_instance(
        instance, "codesage_deep", tmp_path, tmp_path, {}, 1, True,
    )
    assert result["status"] == "preview" and calls == ["deep"]
    assert pipeline.build_parser().parse_args([
        "run", "--stage", "review", "--reviewer", "codesage_deep", "--dataset", "data/aacr_bench.jsonl",
    ]).allow_model_calls is False


def test_zero_findings_and_file_level_location_are_not_invented(tmp_path: Path) -> None:
    result = codesage_deep._load_result(
        _write_result(tmp_path, _final_payload()), codesage_deep.DEFAULT_BACKEND_ROOT,
    )
    assert codesage_deep.result_to_comments(result) == []
    payload = _final_payload(findings=[{
        "file_path": "src/a.py", "line_start": None, "line_end": None,
        "severity": "low", "title": "File-level contract", "body": "Concrete issue.",
    }])
    result = codesage_deep._load_result(_write_result(tmp_path, payload), codesage_deep.DEFAULT_BACKEND_ROOT)
    comment = codesage_deep.result_to_comments(result)[0]
    assert comment["start_line"] is None and comment["end_line"] is None


def test_existing_reviewer_dispatch_is_unchanged(monkeypatch, tmp_path: Path) -> None:
    called: list[str] = []
    for name, module in (
        ("ocr", pipeline.ocr_reviewer),
        ("codex", pipeline.codex_reviewer),
        ("claude", pipeline.claude_reviewer),
    ):
        monkeypatch.setattr(module, "review_instance", lambda *, _name=name, **_kwargs: called.append(_name) or {"status": "ok"})
    for name in ("ocr", "codex", "claude"):
        result = pipeline._review_one_instance(_instance(), name, tmp_path, tmp_path, {}, 1, True)
        assert result["status"] == "ok"
    assert called == ["ocr", "codex", "claude"]


def test_adapter_timeout_leaves_only_diagnostic(tmp_path: Path, monkeypatch) -> None:
    instance = _instance()
    monkeypatch.setattr(codesage_deep, "prepare_repo", lambda **_kwargs: tmp_path)

    def timeout(command: list[str], **_kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(codesage_deep.subprocess, "run", timeout)
    results_dir = tmp_path / "results"
    summary = codesage_deep.review_instance(instance, tmp_path / "repo", results_dir, allow_model_calls=True)
    assert summary["status"] == "failed" and summary["error"] == "timeout"
    assert not config.result_path(results_dir, instance.instance_id).exists()
    assert (Path(summary["artifact_dir"]) / "diagnostic.json").is_file()


def _write_result(directory: Path, payload: dict) -> Path:
    path = directory / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
