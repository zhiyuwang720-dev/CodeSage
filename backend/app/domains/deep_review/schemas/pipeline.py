from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .input import FileChange


class SemanticBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narrative: str = ""
    stated_intent: list[str] = Field(default_factory=list)
    implemented_intent: list[str] = Field(default_factory=list)
    intent_gaps: list[str] = Field(default_factory=list)
    unrelated_changes: list[str] = Field(default_factory=list)
    risk_surfaces: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class Anatomy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[FileChange] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)
    related_paths: list[str] = Field(default_factory=list)
    summary: str = ""


class ReviewDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    review_prompt: str
    target_files: list[str] = Field(default_factory=list)
    context_files: list[str] = Field(default_factory=list)
    priority: int = Field(default=1, ge=1, le=10)
    fallback: bool = False
    deferred: bool = False


class ReviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = ""
    dimensions: list[ReviewDimension] = Field(default_factory=list)
    cross_reference_hints: list[str] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    severity: Literal["critical", "high", "medium", "low"]
    title: str
    body: str
    evidence: str = ""
    suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    tags: list[str] = Field(default_factory=list)
    dimension_name: str = ""


class ReviewerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str = ""


class FindingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_index: int = Field(ge=0)
    result: Literal["keep", "drop"]
    reason: str = ""
    revised_severity: Literal["critical", "high", "medium", "low"] | None = None


class CrossAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[FindingDecision] = Field(default_factory=list)
    new_findings: list[ReviewFinding] = Field(default_factory=list)
    unresolved_risks: list[str] = Field(default_factory=list)
    summary: str = ""

