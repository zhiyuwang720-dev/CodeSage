"""Minimal messages exchanged between the control plane and agent nodes."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TRACE_KEYS = {"traceparent", "tracestate", "baggage"}


def _uuid(value: str) -> str:
    return str(UUID(str(value)))


class _BoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InputArtifactRef(_BoundaryModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=128)


class ResultManifestRef(_BoundaryModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=128)


class RunCommand(_BoundaryModel):
    schema_version: Literal[1] = 1
    task_id: str
    agent_type: str
    entrypoint: str
    agent_version: str = Field(min_length=1, max_length=64)
    delivery_id: str
    input_ref: InputArtifactRef
    deadline_at: datetime | None = None
    grant_ref: str = Field(min_length=1, max_length=128)
    trace_carrier: dict[str, str] = Field(default_factory=dict)

    @field_validator("task_id", "delivery_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _uuid(value)

    @field_validator("agent_type", "entrypoint")
    @classmethod
    def validate_names(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("agent_type/entrypoint must be bounded symbolic names")
        return value

    @field_validator("deadline_at")
    @classmethod
    def validate_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("deadline_at must include a UTC offset")
        return value

    @field_validator("trace_carrier")
    @classmethod
    def validate_trace_carrier(cls, value: dict[str, str]) -> dict[str, str]:
        if not set(value) <= _TRACE_KEYS:
            raise ValueError("trace_carrier contains an unsupported or credential-like field")
        if sum(len(key) + len(item) for key, item in value.items()) > 4096:
            raise ValueError("trace_carrier exceeds 4096 bytes")
        return value


class AttemptContext(_BoundaryModel):
    schema_version: Literal[1] = 1
    task_id: str
    attempt_id: str
    node_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=1, max_length=128)
    lease_epoch: int = Field(ge=1)
    delivery_id: str
    lease_expires_at: datetime
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("task_id", "attempt_id", "delivery_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _uuid(value)

    @field_validator("lease_expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("lease_expires_at must include a UTC offset")
        return value


class ResultSubmission(_BoundaryModel):
    schema_version: Literal[1] = 1
    task_id: str
    attempt_id: str
    epoch: int = Field(ge=1)
    operation: Literal["checkpoint", "final"]
    idempotency_key: str = Field(min_length=1, max_length=128)
    result_schema: str = Field(min_length=1, max_length=128)
    manifest_ref: ResultManifestRef
    outcome: Literal["completed", "incomplete", "failed", "cancelled"]

    @field_validator("task_id", "attempt_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _uuid(value)


class SubmissionReceipt(_BoundaryModel):
    schema_version: Literal[1] = 1
    receipt_id: str
    task_id: str
    attempt_id: str
    epoch: int = Field(ge=1)
    operation: Literal["checkpoint", "final"]
    accepted_at: datetime
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("receipt_id", "task_id", "attempt_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _uuid(value)

    @field_validator("accepted_at")
    @classmethod
    def validate_accepted_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("accepted_at must include a UTC offset")
        return value
