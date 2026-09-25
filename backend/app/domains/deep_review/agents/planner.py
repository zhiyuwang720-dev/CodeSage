from __future__ import annotations

import asyncio
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.pipeline import Anatomy, ReviewPlan, SemanticBrief
from app.domains.deep_review.services.blast_radius import BlastResult
from app.domains.deep_review.services.head_tree import HeadTreeIndex, validate_head_context_paths
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.plan_repair import repair_plan
from app.domains.deep_review.services.prompt_loader import load_prompt, render_prompt
from app.domains.deep_review.services.runtime import AgentCallResult


BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ReviewDimensionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="Stable short name used by cross-reference hints.",
    )
    review_prompt: str = Field(
        min_length=20,
        max_length=2000,
        description=(
            "Executable investigation task: behavior to trace, contracts to compare, "
            "failure consequence, and when the investigation is complete."
        ),
    )
    target_files: list[str] = Field(
        min_length=1,
        max_length=64,
        description="Repository-relative changed files for which this dimension is responsible.",
    )
    context_files: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="Read-only supporting paths; may repeat across dimensions.",
    )
    priority: int = Field(
        default=5,
        ge=1,
        le=10,
        description="1 is highest priority and 10 is lowest.",
    )
    rationale: str = Field(
        default="",
        max_length=300,
        description="Why this investigation is worth performing.",
    )


class CrossReferenceHintDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_names: list[str] = Field(
        min_length=2,
        max_length=8,
        description="At least two existing dimension names that must be checked together.",
    )
    relation: str = Field(
        min_length=10,
        max_length=500,
        description="Concrete producer/consumer, state, configuration, or contract relation.",
    )
    symbol_or_contract: str = Field(
        min_length=1,
        max_length=200,
        description="Function, type, setting, API, or invariant that locates the relation.",
    )


class ReviewPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=2000, description="Short planning summary.")
    dimensions: list[ReviewDimensionDraft] = Field(
        default_factory=list,
        max_length=32,
        description="Focused investigation groups covering the changed files.",
    )
    cross_reference_hints: list[CrossReferenceHintDraft] = Field(
        default_factory=list,
        max_length=16,
        description="Explicit cross-dimension contracts worth checking later.",
    )
    assumptions: list[BoundedText] = Field(
        default_factory=list,
        max_length=10,
        description="Explicit planning assumptions for later auditability.",
    )


def build_planner_prompt(
    *,
    snapshot: ReviewSnapshot,
    anatomy: Anatomy,
    semantic: SemanticBrief,
    config: DeepReviewConfig,
    blast_result: BlastResult | None = None,
) -> str:
    constraints = {
        "base_commit": snapshot.base_commit,
        "head_commit": snapshot.head_commit,
        "review_paths": snapshot.review_paths,
        "context_paths": snapshot.context_paths,
        "max_planner_dimensions": config.max_planner_dimensions,
        "max_turns_planner": config.max_turns_planner,
        "max_files_per_work_item": config.max_files_per_work_item,
        "max_final_dimensions": config.max_final_dimensions,
    }
    evidence = {
        "pr_metadata": {
            "title": snapshot.input.title[:300],
            "description": snapshot.input.description[:2_000],
            "commit_messages": [message[:200] for message in snapshot.commit_messages[:20]],
        },
        "filter_summary": {
            "review_count": len(snapshot.review_paths),
            "context_count": len(snapshot.context_paths),
            "excluded_count": sum(
                item.action.value == "exclude" for item in snapshot.filter_result.decisions
            ),
        },
        "semantic_brief": semantic.model_dump(mode="json"),
        "anatomy": {
            "stats": anatomy.stats.model_dump(),
            "clusters": [item.model_dump() for item in anatomy.clusters],
        },
        "blast_radius": (blast_result or BlastResult()).prompt_projection(),
        "hints": config.hints,
    }
    return render_prompt(
        "planner_user",
        constraints_json=json.dumps(constraints, ensure_ascii=False, sort_keys=True),
        evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    )


async def run_planner_agent(
    runtime,
    *,
    snapshot: ReviewSnapshot,
    anatomy: Anatomy,
    semantic: SemanticBrief,
    config: DeepReviewConfig,
    tools: list,
    blast_result: BlastResult | None = None,
    head_tree: HeadTreeIndex | None = None,
) -> AgentCallResult[ReviewPlan]:
    harness_result = None
    try:
        harness_result = await runtime.harness(
            build_planner_prompt(
                snapshot=snapshot,
                anatomy=anatomy,
                semantic=semantic,
                config=config,
                blast_result=blast_result,
            ),
            schema=ReviewPlanDraft,
            cwd=snapshot.input.repo_path,
            system_prompt=load_prompt("planner"),
            max_turns=config.max_turns_planner,
            tool_allowlist={"file_read", "file_read_diff", "file_find", "code_search"},
            tools=tools,
        )
        draft = ReviewPlanDraft.model_validate(harness_result.parsed)
        requested_context = {
            path for dimension in draft.dimensions for path in dimension.context_files
        }
        legal_context = await validate_head_context_paths(
            snapshot.input.repo_path, snapshot.head_commit, requested_context, config,
            head_tree=head_tree,
        )
        plan = repair_plan(
            draft.model_dump(mode="json"), snapshot, config,
            legal_context_paths=legal_context,
        )
        if len(requested_context) > 512:
            plan.repair_actions.append(
                f"context_validation_budget_limited:{len(requested_context)}->512"
            )
        return AgentCallResult(
            value=plan,
            session_id=harness_result.session_id,
            usage=harness_result.usage,
            cost_usd=harness_result.cost_usd,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_kind = getattr(exc, "error_kind", None)
        error = f"{type(exc).__name__}:{error_kind}" if error_kind else type(exc).__name__
        return AgentCallResult(
            value=None,
            error=error,
            session_id=harness_result.session_id if harness_result is not None else getattr(exc, "session_id", None),
            usage=harness_result.usage if harness_result is not None else getattr(exc, "usage", None),
            cost_usd=harness_result.cost_usd if harness_result is not None else getattr(exc, "cost_usd", None),
        )
