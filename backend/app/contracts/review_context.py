"""Trusted PR review context contracts (Plan 21A).

The contracts in this module are pure data.  Runtime paths and credentials are
deliberately absent from every model-visible object.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.review_execution import ArtifactRef


ReviewMode = Literal["diff_only", "repository_required"]
DiffBasis = Literal["two_dot", "merge_base", "provided_patch"]
SourceStatus = Literal["available", "unavailable", "not_requested"]
CapabilityName = Literal[
    "list_changes",
    "read_diff",
    "search_diff",
    "read_source",
    "search_source",
    "finalize_review",
]

CONTEXT_POLICY_VERSION = "21a-v1"
TOOL_PROFILE_VERSION = "pr-review-v1"
DIFF_PARSER_VERSION = "unified-diff-v1"
SNAPSHOT_VERIFICATION_VERSION = "git-object-v1"


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RepositorySnapshotRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    effective_base_sha: str = Field(min_length=7)
    head_sha: str = Field(min_length=7)
    original_base_tip: str | None = None
    diff_basis: DiffBasis
    git_object_format: str = "sha1"
    verification_version: str = SNAPSHOT_VERIFICATION_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    manifest_hash: str = Field(min_length=64, max_length=64)

    @field_validator("effective_base_sha", "head_sha", "original_base_tip")
    @classmethod
    def validate_commit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("Git commit must be hexadecimal")
        return normalized


class ReviewCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str | None = None
    execution_attempt_id: str | None = None
    mode: ReviewMode
    source_status: SourceStatus
    snapshot_id: str | None = None
    capabilities: list[CapabilityName] = Field(default_factory=list)
    reason_code: str | None = None
    limitations: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    worker_id: str = Field(min_length=1)
    policy_version: str = CONTEXT_POLICY_VERSION

    @model_validator(mode="after")
    def validate_source_capabilities(self) -> "ReviewCapabilities":
        normalized = list(dict.fromkeys(self.capabilities))
        self.capabilities = normalized
        source_tools = {"read_source", "search_source"}
        if self.mode == "diff_only" and source_tools.intersection(normalized):
            raise ValueError("diff_only cannot expose source tools")
        if self.source_status == "available" and not self.snapshot_id:
            raise ValueError("available source requires snapshot_id")
        if self.mode == "repository_required" and self.source_status != "available":
            if not self.reason_code:
                raise ValueError("unavailable required source needs reason_code")
        return self


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    evidence_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    kind: Literal["diff", "source"]
    snapshot_id: str | None = None
    side: Literal["base", "head"] | None = None
    commit_sha: str | None = None
    file_id: str | None = None
    path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    hunk_id: str | None = None
    content_sha256: str = Field(min_length=64, max_length=64)
    artifact_ref: ArtifactRef | None = None
    origin: Literal["primed", "tool"]
    producer_version: str = Field(min_length=1)


class ChangeUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_id: str
    file_id: str
    hunk_id: str | None = None
    path: str
    status: Literal["added", "modified", "deleted", "renamed", "binary", "metadata"]
    old_start: int | None = None
    old_count: int | None = None
    new_start: int | None = None
    new_count: int | None = None


class ReviewContextManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str
    diff_ref: ArtifactRef
    diff_sha256: str = Field(min_length=64, max_length=64)
    parser_version: str = DIFF_PARSER_VERSION
    snapshot_ref: RepositorySnapshotRef | None = None
    capabilities: ReviewCapabilities
    file_count: int = Field(ge=0)
    hunk_count: int = Field(ge=0)
    change_units: list[ChangeUnit] = Field(default_factory=list)
    excluded_units: list[dict[str, Any]] = Field(default_factory=list)
    parse_errors: list[dict[str, Any]] = Field(default_factory=list)
    index_ref: ArtifactRef
    manifest_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_identity(self) -> "ReviewContextManifest":
        if self.diff_ref.run_id != self.run_id or self.index_ref.run_id != self.run_id:
            raise ValueError("manifest artifacts must belong to this run")
        if self.diff_ref.sha256 != self.diff_sha256:
            raise ValueError("manifest diff hash mismatch")
        return self


def snapshot_identity_payload(
    *,
    repository_key: str,
    effective_base_sha: str,
    head_sha: str,
    original_base_tip: str | None,
    diff_basis: DiffBasis,
    git_object_format: str,
) -> dict[str, Any]:
    return {
        "repository_key": repository_key,
        "effective_base_sha": effective_base_sha,
        "head_sha": head_sha,
        "original_base_tip": original_base_tip,
        "diff_basis": diff_basis,
        "git_object_format": git_object_format,
        "verification_version": SNAPSHOT_VERIFICATION_VERSION,
    }
