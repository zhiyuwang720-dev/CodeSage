from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GoldenFinding(BaseModel):
    golden_id: str
    comment: str
    severity: str | None = None
    category: str | None = None


class DatasetCase(BaseModel):
    schema_version: Literal["1"] = "1"
    case_id: str
    pr_url: str
    repo: str
    pr_title: str
    golden: list[GoldenFinding]
    source_mode: Literal["full_source", "diff_only", "fixture_unverified"] = "fixture_unverified"
    fixture_path: str | None = None
    fixture_sha256: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    merge_base: str | None = None
    original_base_tip: str | None = None
    diff_sha256: str | None = None
    golden_sha256: str
    changed_lines: int | None = None


class CandidateFinding(BaseModel):
    candidate_id: str
    title: str
    description: str = ""
    severity: str | None = None
    category: str | None = None
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    suggestion: str | None = None
    source: str | None = None


class EvalRunManifest(BaseModel):
    schema_version: Literal["1"] = "1"
    eval_run_id: str
    created_at: datetime = Field(default_factory=utc_now)
    suite: str
    case_ids: list[str]
    dataset_sha256: str
    code_commit: str
    code_dirty_sha256: str
    runner_version: str = "codesage_eval/1.0.0"
    scoring_version: str = "codesage_matching_v1"
    coverage_version: str = "golden_coverage_v1"
    serializer_version: str = "finding_serializer_v1"
    judge_fingerprint: str | None = None
    model_fingerprint: str | None = None
    prompt_fingerprint: str | None = None
    tool_fingerprint: str | None = None
    flow_fingerprint: str | None = None
    price_table_version: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    hardware_fingerprint: str | None = None
    fixture_fingerprints: dict[str, str] = Field(default_factory=dict)
    concurrency: int = 2
    timeout_seconds: int = 3600
    phoenix_dataset_version: str | None = None
    phoenix_experiment_id: str | None = None
    task_run_trace_map: dict[str, dict[str, str | None]] = Field(default_factory=dict)


class EvalCaseResult(BaseModel):
    schema_version: Literal["1"] = "1"
    eval_run_id: str
    case_id: str
    status: Literal["pending", "running", "completed", "failed"]
    execution_complete: bool = False
    quality_complete: bool = False
    trace_complete: bool = False
    usage_complete: bool = False
    task_id: str | None = None
    review_run_id: str | None = None
    trace_id: str | None = None
    error_kind: str | None = None
    candidates: list[CandidateFinding] = Field(default_factory=list)
    telemetry: dict[str, Any] = Field(default_factory=dict)


class JudgmentRecord(BaseModel):
    schema_version: Literal["1"] = "1"
    eval_run_id: str
    case_id: str
    golden_id: str
    candidate_id: str
    matched: bool | None = None
    confidence: float | None = None
    reason: str = ""
    status: Literal["computed", "reused", "unknown"] = "computed"
    judge_fingerprint: str
    cache_key: str
    human_label: Literal["valid_extra", "invalid", "uncertain"] | None = None
