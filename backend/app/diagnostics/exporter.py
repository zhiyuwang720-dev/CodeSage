"""Deterministic offline session bundle export.

This module intentionally does not treat telemetry as business truth. It only
projects locally available evidence and records missing dimensions explicitly.
"""

from __future__ import annotations

import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings

BUNDLE_SCHEMA_VERSION = 1
SPEC_VERSION = "20.2.0"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except json.JSONDecodeError:
            continue
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def _log_rows(run_id: str, root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.jsonl")):
        for row in _read_jsonl(path):
            if str(row.get("review_run_id") or row.get("run_id") or "") == run_id:
                rows.append(row)
    rows.sort(key=lambda row: (str(row.get("timestamp") or ""), int(row.get("process_sequence") or 0)))
    return rows


def _content_files(run_id: str, root: Path) -> list[Path]:
    run_root = root / run_id
    if not run_root.exists():
        return []
    return sorted(path for path in run_root.rglob("*") if path.is_file() and path.name != ".quota.lock")


def _clean_public(value: Any) -> Any:
    if isinstance(value, dict):
        blocked = ("path", "endpoint", "prompt", "content", "exception", "message")
        return {
            key: _clean_public(item)
            for key, item in value.items()
            if not any(token in key.lower() for token in blocked)
        }
    if isinstance(value, list):
        return [_clean_public(item) for item in value]
    if isinstance(value, str):
        return "".join(ch for ch in value if ch.isprintable())[:4096]
    return value


def export_bundle(
    *,
    run_id: str,
    output: str | Path,
    public: bool = False,
    logs_root: str | Path | None = None,
    content_root: str | Path | None = None,
) -> tuple[Path, int]:
    target = Path(output)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"output directory is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)

    logs_path = Path(logs_root or settings.OTEL_LOG_ROOT)
    if not logs_path.is_absolute():
        logs_path = Path.cwd() / logs_path
    content_path = Path(content_root or settings.OTEL_CONTENT_ROOT)
    if not content_path.is_absolute():
        content_path = Path.cwd() / content_path

    logs = _log_rows(run_id, logs_path)
    content_files = _content_files(run_id, content_path)
    timeline = [
        {
            "timestamp": row.get("timestamp"),
            "event_name": row.get("event_name"),
            "source_type": "log",
            "source_id": row.get("process_instance"),
            "review_run_id": row.get("review_run_id"),
            "session_id": row.get("session_id"),
            "turn_id": row.get("turn_id"),
        }
        for row in logs
    ]
    model_calls = [row for row in logs if str(row.get("event_name") or "").startswith(("model.", "provider."))]
    tool_calls = [row for row in logs if "tool" in str(row.get("event_name") or "").lower()]
    findings = [row for row in logs if "finding" in str(row.get("event_name") or "").lower()]
    if public:
        logs = [_clean_public(row) for row in logs]
        timeline = [_clean_public(row) for row in timeline]
        model_calls = [_clean_public(row) for row in model_calls]
        tool_calls = [_clean_public(row) for row in tool_calls]
        findings = [_clean_public(row) for row in findings]

    contents_target = target / "contents"
    if not public and content_files:
        contents_target.mkdir(parents=True, exist_ok=True)
        for source in content_files:
            relative = source.relative_to(content_path / run_id)
            destination = contents_target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    completeness = {
        "execution": {"status": "unknown", "reason": "local-only export has no business DB adapter"},
        "trace": {"status": "unknown", "reason": "Phoenix pagination is checked by integration export"},
        "usage": {"status": "unknown", "missing_ids": [], "reason": "model-call projection not queried"},
        "content": {"status": "complete" if content_files else "not_applicable", "files": len(content_files)},
        "logs": {"status": "complete" if logs else "partial", "rows": len(logs)},
        "pricing": {"status": "unknown", "reason": "catalog source not embedded"},
        "result_provenance": {"status": "partial" if findings else "unknown", "rows": len(findings)},
    }
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "spec_version": SPEC_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cutoff": datetime.now(timezone.utc).isoformat(),
        "visibility": "public" if public else "local",
        "sources": {
            "logs_root": None if public else str(logs_path),
            "content_root": None if public else str(content_path),
        },
        "files": {},
    }

    _write_jsonl(target / "timeline.jsonl", timeline)
    _write_jsonl(target / "logs.jsonl", logs)
    _write_jsonl(target / "model-calls.jsonl", model_calls)
    _write_jsonl(target / "tool-calls.jsonl", tool_calls)
    _write_jsonl(target / "findings-decisions.jsonl", findings)
    _write_json(target / "metrics-summary.json", {"status": "unknown", "reason": "no metrics snapshot supplied"})
    _write_json(target / "completeness.json", completeness)

    rows_text = json.dumps(
        {
            "run_id": run_id,
            "logs": len(logs),
            "model_calls": len(model_calls),
            "tool_calls": len(tool_calls),
            "findings": len(findings),
            "content_files": len(content_files),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    report = (
        "<!doctype html><html><head><meta charset='utf-8'><title>CodeSage diagnostics</title></head>"
        "<body><h1>CodeSage diagnostics</h1><pre>"
        + html.escape(rows_text)
        + "</pre></body></html>"
    )
    (target / "report.html").write_text(report, encoding="utf-8")

    for path in sorted(target.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            import hashlib

            manifest["files"][str(path.relative_to(target)).replace("\\", "/")] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    _write_json(target / "manifest.json", manifest)

    partial = any(item.get("status") in {"partial", "unknown"} for item in completeness.values())
    return target, 2 if partial else 0


def summarize_bundle(input_dir: str | Path) -> dict[str, Any]:
    root = Path(input_dir)
    summary: dict[str, Any] = {"input": str(root), "files": {}}
    for name in (
        "timeline.jsonl",
        "logs.jsonl",
        "model-calls.jsonl",
        "tool-calls.jsonl",
        "findings-decisions.jsonl",
    ):
        summary["files"][name] = len(_read_jsonl(root / name))
    completeness = json.loads((root / "completeness.json").read_text(encoding="utf-8"))
    summary["completeness"] = completeness
    return summary
