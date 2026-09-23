from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from .pipeline import ChangeCluster, DiffStats, ReviewFinding, ReviewPlan, SemanticBrief


class ReviewMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewed_files: int = Field(default=0, ge=0)
    excluded_files: int = Field(default=0, ge=0)
    dimensions: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    usage_complete: bool = False
    cost_available: bool = False
    duration_ms: int = Field(default=0, ge=0)


class DeepReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: Literal["completed", "partial", "failed"]
    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str = ""
    unresolved_risks: list[str] = Field(default_factory=list)
    metrics: ReviewMetrics


class AgentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["semantic", "planning", "reviewer"]
    dimension_name: str | None = None
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=500)


class ReviewerDimensionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension_name: str
    dimension_order: int = Field(ge=0)
    status: Literal["succeeded", "failed", "deferred", "degraded"]
    target_files: list[str] = Field(default_factory=list)
    context_files: list[str] = Field(default_factory=list)
    summary: str = ""
    finding_count: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=500)
    diagnostics: list[str] = Field(default_factory=list)


class PreparationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    mode: Literal["preparation"]
    pipeline_complete: Literal[False]
    completed_stage: Literal["anatomy", "planning", "review"]
    base_commit: str = Field(min_length=7)
    head_commit: str = Field(min_length=7)
    merge_base: str = Field(min_length=7)
    review_paths: list[str] = Field(default_factory=list)
    context_paths: list[str] = Field(default_factory=list)
    excluded_count: int = Field(default=0, ge=0)
    stats: DiffStats
    clusters: list[ChangeCluster] = Field(default_factory=list)
    related_paths: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    semantic: SemanticBrief | None = None
    plan: ReviewPlan | None = None
    reviewers: list[ReviewerDimensionReport] = Field(default_factory=list)
    candidates: list[ReviewFinding] = Field(default_factory=list)
    candidate_count: int = Field(default=0, ge=0)
    reviewer_dimensions_started: int = Field(default=0, ge=0)
    reviewer_dimensions_succeeded: int = Field(default=0, ge=0)
    reviewer_dimensions_failed: int = Field(default=0, ge=0)
    reviewer_dimensions_deferred: int = Field(default=0, ge=0)
    reviewer_dimensions_degraded: int = Field(default=0, ge=0)
    agent_observations: list[AgentObservation] = Field(default_factory=list)
