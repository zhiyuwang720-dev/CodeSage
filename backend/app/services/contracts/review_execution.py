"""快速 PR 审查执行控制面的纯数据契约。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.contracts.final_review_contract import ReviewFinding


Sha256 = str
SourceKind = Literal["git", "diff"]
StageExecutionStatus = Literal["completed", "incomplete", "failed", "cancelled"]


def _validate_sha256(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        raise ValueError(f"{field_name} 必须是 64 位 SHA-256 十六进制字符串")
    return normalized


class ReviewRunIdentity(BaseModel):
    """一次逻辑审查的稳定身份；attempt 变化不改变此对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    source_kind: SourceKind
    repository_key: str = Field(min_length=1)
    pr_number: int | None = Field(default=None, ge=1)
    base_sha: str | None = None
    head_sha: str | None = None
    diff_sha256: Sha256
    config_fingerprint: Sha256

    @field_validator("diff_sha256", "config_fingerprint")
    @classmethod
    def validate_hashes(cls, value: str, info) -> str:
        return _validate_sha256(value, info.field_name)

    @field_validator("base_sha", "head_sha")
    @classmethod
    def normalize_git_sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("Git SHA 必须是非空十六进制字符串")
        return normalized

    @model_validator(mode="after")
    def require_fixed_git_range(self) -> "ReviewRunIdentity":
        if self.source_kind == "git" and (not self.base_sha or not self.head_sha):
            raise ValueError("Git 输入必须同时提供固定 base_sha 和 head_sha")
        return self


class ExecutionContext(BaseModel):
    """可信运行时上下文。目录和 deadline 不应注入模型消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: ReviewRunIdentity
    attempt_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=1)
    workspace_root: str = Field(min_length=1)
    artifact_root: str = Field(min_length=1)
    deadline_at: datetime

    @field_validator("workspace_root", "artifact_root")
    @classmethod
    def require_absolute_runtime_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("运行时目录必须是绝对路径")
        return str(path)


class ArtifactRef(BaseModel):
    """run 根内不可变产物的内容寻址引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    artifact_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_sha256(value, "sha256")

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or normalized.startswith("/")
            or len(path.parts) == 0
            or any(part in ("", ".", "..") for part in path.parts)
            or ":" in path.parts[0]
        ):
            raise ValueError("relative_path 必须是 run 根内的安全相对路径")
        return path.as_posix()


class StageResult(BaseModel):
    """阶段 Checkpoint 的规范载荷。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    stage_type: str = Field(min_length=1)
    status: StageExecutionStatus
    session_id: str | None = None
    findings: list[ReviewFinding] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_error_shape(self) -> "StageResult":
        if self.status in ("failed", "incomplete") and not (
            self.error_code or self.error_message
        ):
            raise ValueError(f"{self.status} 阶段必须包含明确错误原因")
        if any(ref.run_id != self.run_id for ref in self.artifact_refs):
            raise ValueError("StageResult 不能引用其他 run 的产物")
        return self


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_config_fingerprint(config: dict[str, Any]) -> str:
    """对调用方筛选后的兼容配置做稳定哈希；凭据不得传入。"""

    forbidden = {"api_key", "token", "secret", "password", "authorization"}

    def reject_secrets(value: Any, path: str = "config") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in forbidden or any(
                    marker in str(key).lower() for marker in ("password", "secret", "api_key")
                ):
                    raise ValueError(f"配置指纹禁止包含凭据字段: {path}.{key}")
                reject_secrets(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                reject_secrets(child, f"{path}[{index}]")

    reject_secrets(config)
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(encoded)
