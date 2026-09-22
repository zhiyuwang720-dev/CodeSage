from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class DeepReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_duration_seconds: int = Field(default=1800, ge=1)
    max_concurrent_reviewers: int = Field(default=4, ge=1)
    max_planner_dimensions: int = Field(default=8, ge=1)
    max_final_dimensions: int = Field(default=9, ge=1)
    max_files_per_work_item: int = Field(default=12, ge=1)
    max_turns_planner: int = Field(default=12, ge=1)
    max_turns_reviewer: int = Field(default=12, ge=1)
    max_turns_cross_analysis: int = Field(default=12, ge=1)
    max_final_findings: int | None = Field(default=None, ge=1)
    max_diff_bytes: int = Field(default=2_000_000, ge=1)
    max_file_bytes: int = Field(default=1_000_000, ge=1)
    max_tool_read_lines: int = Field(default=400, ge=1)
    max_tool_output_bytes: int = Field(default=100_000, ge=512)
    max_import_graph_files: int = Field(default=2_000, ge=1)
    max_import_scan_bytes: int = Field(default=32_000_000, ge=1)
    max_search_results: int = Field(default=100, ge=1)
    tool_timeout_seconds: int = Field(default=10, ge=1)
    max_cross_context_bytes: int | None = Field(default=None, ge=1)
    max_candidate_count: int | None = Field(default=None, ge=1)
    cluster_directory_depth: int = Field(default=1, ge=1)
    max_cluster_files: int = Field(default=24, ge=1)
    max_evidence_bytes_per_finding: int = Field(default=24_000, ge=512)
    min_severity: Literal["critical", "high", "medium", "low"] = "low"
    semantic_role: str = "deep_review:semantic"
    planner_role: str = "deep_review:planner"
    reviewer_role: str = "deep_review:reviewer"
    cross_analysis_role: str = "deep_review:cross_analysis"
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)

    @field_validator("semantic_role", "planner_role", "reviewer_role", "cross_analysis_role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("role cannot be empty")
        return normalized

    @field_validator("include_paths", "exclude_paths", "hints")
    @classmethod
    def _validate_strings(cls, value: list[str]) -> list[str]:
        return [item for item in value if item.strip()]
