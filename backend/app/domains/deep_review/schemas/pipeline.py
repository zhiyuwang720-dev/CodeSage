from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .input import FileChange


class SemanticBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["model", "fallback"] = "model"
    narrative: str = ""
    stated_intent: list[str] = Field(default_factory=list)
    implemented_intent: list[str] = Field(default_factory=list)
    intent_gaps: list[str] = Field(default_factory=list)
    unrelated_changes: list[str] = Field(default_factory=list)
    risk_surfaces: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class DiffHunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_start: int
    old_count: int = 1
    new_start: int
    new_count: int = 1
    header: str = ""
    content: str = ""


class DiffStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_files: int = 0
    total_additions: int = 0
    total_deletions: int = 0
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    files_renamed: int = 0
    files_binary: int = 0
    test_files: int = 0
    test_to_code_ratio: float = 0.0


class ChangeCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    files: list[str] = Field(default_factory=list)
    primary_language: str = ""


class CrossReferenceHint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension_names: list[str] = Field(min_length=2)
    relation: str
    symbol_or_contract: str = ""


class Anatomy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[FileChange] = Field(default_factory=list)
    hunks: dict[str, list[DiffHunk]] = Field(default_factory=dict)
    stats: DiffStats = Field(default_factory=DiffStats)
    clusters: list[ChangeCluster] = Field(default_factory=list)
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
    source: Literal["model", "fallback", "merged", "split"] = "model"


class ReviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = ""
    dimensions: list[ReviewDimension] = Field(default_factory=list)
    cross_reference_hints: list[CrossReferenceHint] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    coverage_complete: bool = False
    repair_actions: list[str] = Field(default_factory=list)
    unresolved_risks: list[str] = Field(default_factory=list)


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


class EvidencePackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_index: int = Field(default=0, ge=0)
    primary_code: str = ""
    caller_snippets: list[str] = Field(default_factory=list)
    cross_ref_snippets: list[str] = Field(default_factory=list)
    diff_hunk: str = ""
    import_context: str = ""
    related_code: str = ""
