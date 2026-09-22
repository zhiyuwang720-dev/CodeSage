from __future__ import annotations

from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field


class ChangeType(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    BINARY = "binary"


class FilterAction(StrEnum):
    REVIEW = "review"
    CONTEXT_ONLY = "context_only"
    EXCLUDE = "exclude"


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_path: str
    base_ref: str
    head_ref: str
    title: str = ""
    description: str = ""
    commit_messages: list[str] = Field(default_factory=list)


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    old_path: str | None = None
    change_type: ChangeType
    diff: str = ""
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)


class FilterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    action: FilterAction
    reason: str

