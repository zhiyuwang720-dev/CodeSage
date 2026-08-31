"""FinalReview 契约(阶段 02 §3.4): PR 审查终结 schema, 对齐 benchmark 类别。

结构对应被替换的 final_finding_contract(FinalizedFindingPayload):
- ReviewFinding: 单条评论候选(富字段, Agent 视角产出)
- FinalReviewPayload: 终结工具 FinalizeReview 的入参
- 综合层(synthesizer)把 ReviewFinding 映射为 pr_review.models.ReviewComment(输出契约)
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReviewSeverity = Literal["low", "medium", "high", "critical"]
ReviewCategory = Literal[
    "bug",
    "security",
    "concurrency",
    "data",
    "api",
    "perf",
    "test_gap",
    "doc_defect",
]
ReviewPerspective = Literal["security", "architecture", "quality", "rules"]
ReviewVerdict = Literal["confirmed", "suspected", "info"]

SEVERITY_RANK: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}


class ReviewFinding(BaseModel):
    """单条 PR 审查评论候选(终结工具的最小单元)。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    severity: ReviewSeverity
    category: ReviewCategory
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    code_snippet: str | None = None
    suggestion: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    needs_verification: bool
    verdict: ReviewVerdict
    source: ReviewPerspective  # 视角标记(spec 03 分视角评估归因依据)

    @field_validator("file_path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        path = value.strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"file_path 必须是仓库相对路径: {value!r}")
        return path

    @field_validator("line_end")
    @classmethod
    def _check_line_range(cls, value: int, info):
        start = info.data.get("line_start")
        if start is not None and value < start:
            raise ValueError("line_end 不能小于 line_start")
        return value

    def dedup_key(self) -> tuple[str, int, str]:
        """综合层去重键(§3.3: file_path + line + category)。"""
        return (self.file_path, self.line_start, self.category)


class FinalReviewPayload(BaseModel):
    """FinalizeReview 终结工具入参: 评论集 + 审查摘要。"""

    model_config = ConfigDict(extra="forbid")

    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str = Field(min_length=1, description="审查覆盖范围与结论; 无评论时必填")


def format_validation_errors(exc) -> list[dict[str, Any]]:
    """pydantic ValidationError → [{loc, msg}](与 final_finding_contract 同型)。"""
    errors: list[dict[str, Any]] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
        errors.append({"loc": loc or "payload", "msg": str(error.get("msg", ""))})
    return errors
