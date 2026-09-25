"""Thin AACR adapter for the existing CodeSageDeep CLI and review algorithm."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import config
from repo_utils import prepare_repo
from schema import ReviewInstance


DEFAULT_BACKEND_ROOT = Path(__file__).resolve().parents[3] / "backend"


def _redact(value: str) -> str:
    text = value[:20_000]
    for key, secret in os.environ.items():
        sensitive = any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        credential_url = key.upper().endswith(("_URL", "_DSN")) and "://" in secret and "@" in secret
        if (sensitive or credential_url) and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
            if credential_url:
                try:
                    password = urlsplit(secret).password
                except ValueError:
                    password = None
                if password and len(password) >= 4:
                    text = text.replace(password, "[REDACTED]")
    return text


def result_to_comments(result: Any) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for finding in result.findings:
        sections = [f"[{finding.severity}] {finding.title}", finding.body]
        if finding.evidence:
            sections.append(f"Evidence: {finding.evidence}")
        if finding.suggestion:
            sections.append(f"Suggestion: {finding.suggestion}")
        comments.append({
            "path": finding.file_path,
            "start_line": finding.line_start,
            "end_line": finding.line_end,
            "content": "\n\n".join(section for section in sections if section),
        })
    return comments


def _result_schema(backend_root: Path) -> Any:
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    from app.domains.deep_review.schemas.output import DeepReviewResult

    return DeepReviewResult


def _load_result(path: Path, backend_root: Path) -> Any:
    DeepReviewResult = _result_schema(backend_root)

    return DeepReviewResult.model_validate_json(path.read_text(encoding="utf-8"))


def _write_diagnostic(case_dir: Path, reason: str, *, exit_code: int | None, stderr: str) -> None:
    (case_dir / "diagnostic.json").write_text(
        json.dumps({"status": "failed", "reason": reason, "exit_code": exit_code,
                    "stderr": _redact(stderr)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def review_instance(
    instance: ReviewInstance,
    repo_dir: Path,
    results_dir: Path,
    *,
    timeout_minutes: int = 30,
    preview: bool = False,
    allow_model_calls: bool = False,
    python_executable: str | None = None,
    backend_root: Path | None = None,
) -> dict[str, Any]:
    """Run one fixed AACR case; only the CodeSageDeep CLI performs review."""
    if preview:
        return {"instance_id": instance.instance_id, "status": "preview",
                "reviewer": "codesage_deep", "model_started": False}
    if not allow_model_calls:
        return {"instance_id": instance.instance_id, "status": "failed",
                "error": "codesage_deep requires explicit --allow-model-calls"}
    output_path = config.result_path(results_dir, instance.instance_id)
    if output_path.exists():
        return {"instance_id": instance.instance_id, "status": "failed",
                "error": "result already exists; choose a new AACR run directory"}

    backend = (backend_root or DEFAULT_BACKEND_ROOT).resolve()
    try:
        _result_schema(backend)
    except (ImportError, OSError) as exc:
        return {"instance_id": instance.instance_id, "status": "failed",
                "error": f"backend schema unavailable: {type(exc).__name__}"}
    python = python_executable or sys.executable
    repo_path = prepare_repo(
        repo_dir=repo_dir,
        clone_url=instance.resolved_clone_url,
        repo_full_name=instance.repo,
        base_commit=instance.base_commit,
        head_commit=instance.head_commit,
    ).resolve()
    safe_case = re.sub(r"[^A-Za-z0-9._-]", "_", config.safe_id(instance.instance_id)).strip("._") or "case"
    case_dir = results_dir.resolve() / "codesage_deep_artifacts" / f"{safe_case}-{uuid.uuid4().hex[:12]}"
    case_dir.mkdir(parents=True, exist_ok=False)
    raw_path = case_dir / "raw-result.json"
    domain_dir = case_dir / "domain"
    command = [
        python, "-m", "app.domains.deep_review",
        "--repo", str(repo_path), "--base", instance.base_commit,
        "--head", instance.head_commit, "--through", "final",
        "--allow-model-calls", "--output", str(raw_path),
        "--store-dir", str(domain_dir),
    ]
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=backend, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_minutes * 60 + 30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_diagnostic(case_dir, "timeout", exit_code=None, stderr=str(exc))
        return {"instance_id": instance.instance_id, "status": "failed",
                "error": "timeout", "artifact_dir": str(case_dir)}
    except OSError as exc:
        _write_diagnostic(case_dir, f"launch_failed:{type(exc).__name__}", exit_code=None, stderr=str(exc))
        return {"instance_id": instance.instance_id, "status": "failed",
                "error": "launch_failed", "artifact_dir": str(case_dir)}
    duration = time.monotonic() - started
    if completed.returncode != 0:
        _write_diagnostic(case_dir, "nonzero_exit", exit_code=completed.returncode, stderr=completed.stderr)
        return {"instance_id": instance.instance_id, "status": "failed",
                "exit_code": completed.returncode, "artifact_dir": str(case_dir)}
    try:
        result = _load_result(raw_path, backend)
        if result.status == "failed":
            raise ValueError("final result reports failed")
        process_path = domain_dir / result.run_id / "process_report.json"
        if not process_path.is_file():
            raise ValueError("process_report.json missing")
        process = json.loads(process_path.read_text(encoding="utf-8"))
        if (process.get("run_id"), process.get("final_status"), process.get("final_content_hash")) != (
            result.run_id, result.status, result.content_hash,
        ):
            raise ValueError("process report identity differs from final result")
        comments = result_to_comments(result)
    except (OSError, ValueError, KeyError, TypeError, ImportError, AttributeError) as exc:
        _write_diagnostic(case_dir, f"invalid_result:{type(exc).__name__}",
                          exit_code=completed.returncode, stderr=completed.stderr)
        return {"instance_id": instance.instance_id, "status": "failed",
                "error": "invalid_result", "artifact_dir": str(case_dir)}

    envelope = {
        "instance_id": instance.instance_id, "repo": instance.repo,
        "base_commit": instance.base_commit, "head_commit": instance.head_commit,
        "reviewer": "codesage_deep", "started_at": started_at,
        "duration_seconds": round(duration, 2), "codesage_deep_exit_code": completed.returncode,
        "review": {"comments": comments, "summary": result.summary, "status": result.status},
        "deep_review": {"run_id": result.run_id, "status": result.status,
                        "content_hash": result.content_hash,
                        "raw_result_path": str(raw_path), "process_report_path": str(process_path),
                        "metrics": result.metrics.model_dump(mode="json")},
        "stderr": _redact(completed.stderr),
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    except OSError as exc:
        _write_diagnostic(case_dir, f"envelope_write_failed:{type(exc).__name__}",
                          exit_code=completed.returncode, stderr=str(exc))
        return {"instance_id": instance.instance_id, "status": "failed",
                "error": "envelope_write_failed", "artifact_dir": str(case_dir)}
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"instance_id": instance.instance_id,
            "status": "ok" if result.status == "completed" else "partial",
            "result_path": str(output_path), "artifact_dir": str(case_dir),
            "run_id": result.run_id, "finding_count": len(comments)}
