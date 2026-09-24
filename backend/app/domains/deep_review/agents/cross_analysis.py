"""One structured Harness pass for evidence, challenge and cross-file analysis."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.deep_review.agents.reviewer import (
    _REVIEW_TEXT_LIMITS, _head_line_counts, _is_persistence_failure,
    _safe_error_message, _truncate_fields,
)
from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.output import ReviewerDimensionReport
from app.domains.deep_review.schemas.pipeline import (
    Anatomy, CrossAnalysisResult, EvidencePackage, ReviewFinding, ReviewPlan, SemanticBrief,
)
from app.domains.deep_review.services.cross_repair import repair_cross_result
from app.domains.deep_review.services.directory_filter import FilterError, normalize_path
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.output_formatter import format_cross_inputs
from app.domains.deep_review.services.prompt_loader import load_prompt, render_prompt
from app.domains.deep_review.services.runtime import AgentCallResult, DeepReviewRuntimeFactory
from app.domains.deep_review.tools.catalog import build_review_tools


class FindingDecisionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_index: int = Field(ge=0, description="Zero-based index in this Cross request's complete candidate list.")
    result: Literal["keep", "drop"] = Field(description="Keep a plausible defect or drop a contradicted/nonactionable claim.")
    reason: str = Field(description="Short, checkable code reason; for duplicates cite the representative index.")
    revised_severity: Literal["critical", "high", "medium", "low"] | None = Field(
        default=None, description="Corrected severity when the candidate's impact was misrated."
    )

    @field_validator("reason")
    @classmethod
    def bound_reason(cls, value: str) -> str:
        return value[:1000]


class CrossFindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str = Field(min_length=1, description="Changed review file containing the primary failure location.")
    line_start: int | None = Field(default=None, ge=1, description="Line in the fixed head snapshot.")
    line_end: int | None = Field(default=None, ge=1, description="Last affected head line, if known.")
    severity: Literal["critical", "high", "medium", "low"] = Field(description="Impact of the verified compound defect.")
    title: str = Field(min_length=5, description="Specific new failure mechanism, not a restatement of a kept candidate.")
    body: str = Field(min_length=20, description="Trigger, cross-file mechanism and concrete consequence.")
    evidence: str = Field(default="", description="Code facts at both ends of the relation.")
    suggestion: str = Field(default="", description="Direct repair direction, if established.")
    confidence: float = Field(default=0.5, ge=0, le=1, description="Confidence in the compound finding.")
    tags: list[str] = Field(default_factory=list, description="Short retrieval tags.")

    @model_validator(mode="before")
    @classmethod
    def bound_prose(cls, value: object) -> object:
        return _truncate_fields(value, _REVIEW_TEXT_LIMITS)


class CrossAnalysisResultDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[FindingDecisionDraft] = Field(
        default_factory=list, description="One decision for every input candidate index; no fixed count cap."
    )
    new_findings: list[CrossFindingDraft] = Field(
        default_factory=list, description="Only independently evidenced compound defects, possibly empty."
    )
    unresolved_risks: list[str] = Field(
        default_factory=list, description="Evidence gaps or conflicting facts that could not be resolved."
    )
    summary: str = Field(default="", description="Concise verified outcome and material coverage limits.")

    @model_validator(mode="before")
    @classmethod
    def bound_summary(cls, value: object) -> object:
        return _truncate_fields(value, {"summary": 2000})


@dataclass(slots=True)
class CrossAgentOutcome:
    call: AgentCallResult[CrossAnalysisResult]
    diagnostics: list[str] = field(default_factory=list)
    partial: bool = False


async def run_cross_agent(
    runtime_factory: DeepReviewRuntimeFactory,
    *,
    snapshot: ReviewSnapshot,
    anatomy: Anatomy,
    semantic: SemanticBrief,
    plan: ReviewPlan,
    reviewers: list[ReviewerDimensionReport],
    candidates: list[ReviewFinding],
    evidence: dict[int, EvidencePackage],
    config: DeepReviewConfig,
) -> CrossAgentOutcome:
    harness_result = None
    try:
        if config.max_candidate_count is not None and len(candidates) > config.max_candidate_count:
            raise ValueError("cross_candidate_capacity_exceeded")
        max_turns = config.cross_turn_limit(len(candidates))
        prompt = render_prompt(
            "cross_analysis_user",
            **format_cross_inputs(
                snapshot=snapshot, anatomy=anatomy, semantic=semantic, plan=plan,
                reviewers=reviewers, candidates=candidates, evidence=evidence,
                max_turns=max_turns,
            ),
        )
        if config.max_cross_context_bytes is not None and len(prompt.encode("utf-8")) > config.max_cross_context_bytes:
            raise ValueError("cross_context_capacity_exceeded")
        runtime = runtime_factory.for_role(config.cross_analysis_role)
        harness_result = await runtime.harness(
            prompt,
            schema=CrossAnalysisResultDraft,
            cwd=snapshot.input.repo_path,
            system_prompt=load_prompt("cross_analysis"),
            max_turns=max_turns,
            tool_allowlist={"file_read", "file_read_diff", "file_find", "code_search"},
            tools=build_review_tools(
                repo_path=snapshot.input.repo_path,
                head_commit=snapshot.head_commit,
                snapshot=snapshot,
                config=config,
            ),
        )
        draft = CrossAnalysisResultDraft.model_validate(harness_result.parsed)
        reported_paths: set[str] = set()
        for finding in draft.new_findings:
            try:
                path = normalize_path(finding.file_path)
            except (FilterError, TypeError, ValueError):
                continue
            if path in snapshot.review_paths:
                reported_paths.add(path)
        line_counts = await _head_line_counts(snapshot, sorted(reported_paths), config)
        repaired = repair_cross_result(
            draft.model_dump(mode="json"), candidates=candidates,
            snapshot=snapshot, head_line_counts=line_counts,
        )
        return CrossAgentOutcome(
            call=AgentCallResult(
                value=repaired.result, session_id=harness_result.session_id,
                usage=harness_result.usage, cost_usd=harness_result.cost_usd,
            ),
            diagnostics=repaired.diagnostics,
            partial=repaired.partial,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if _is_persistence_failure(exc):
            raise
        return CrossAgentOutcome(
            call=AgentCallResult(
                value=None, error=_safe_error_message(exc),
                session_id=harness_result.session_id if harness_result is not None else getattr(exc, "session_id", None),
                usage=harness_result.usage if harness_result is not None else getattr(exc, "usage", None),
                cost_usd=harness_result.cost_usd if harness_result is not None else getattr(exc, "cost_usd", None),
            ),
            partial=True,
        )
