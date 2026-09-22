from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .pipeline import ReviewFinding


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

