from __future__ import annotations

import asyncio
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.pipeline import Anatomy, ReviewPlan, SemanticBrief
from app.domains.deep_review.services.directory_filter import DirectoryFilter, normalize_path, FilterError
from app.domains.deep_review.services.git_runner import run_git_output_bounded
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.plan_repair import repair_plan
from app.domains.deep_review.services.prompt_loader import load_prompt, render_prompt
from app.domains.deep_review.services.runtime import AgentCallResult


BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


async def _head_context_paths(snapshot: ReviewSnapshot, config: DeepReviewConfig) -> set[str]:
    result = await run_git_output_bounded(
        snapshot.input.repo_path,
        ["ls-tree", "-r", "-z", snapshot.head_commit],
        max_bytes=config.max_import_tree_bytes,
        timeout_seconds=config.tool_timeout_seconds,
    )
    if result.returncode != 0 or result.truncated:
        raise RuntimeError("head tree listing unavailable or exceeds max_import_tree_bytes")
    secret_filter = DirectoryFilter(config)
    paths: set[str] = set()
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        header, separator, value = entry.partition("\t")
        if not separator or not header.startswith("100"):
            continue
        try:
            path = normalize_path(value)
        except FilterError:
            continue
        if not secret_filter.is_secret_path(path):
            paths.add(path)
    return paths


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
            "related_paths": anatomy.related_paths,
        },
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
) -> AgentCallResult[ReviewPlan]:
    harness_result = None
    try:
        legal_context = await _head_context_paths(snapshot, config)
        harness_result = await runtime.harness(
            build_planner_prompt(
                snapshot=snapshot,
                anatomy=anatomy,
                semantic=semantic,
                config=config,
            ),
            schema=ReviewPlanDraft,
            cwd=snapshot.input.repo_path,
            system_prompt=load_prompt("planner"),
            max_turns=config.max_turns_planner,
            tool_allowlist={"file_read", "file_read_diff", "file_find", "code_search"},
            tools=tools,
        )
        draft = ReviewPlanDraft.model_validate(harness_result.parsed)
        plan = repair_plan(
            draft.model_dump(mode="json"), snapshot, config,
            legal_context_paths=legal_context,
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
        return AgentCallResult(
            value=None,
            error=type(exc).__name__,
            session_id=harness_result.session_id if harness_result is not None else getattr(exc, "session_id", None),
            usage=harness_result.usage if harness_result is not None else getattr(exc, "usage", None),
            cost_usd=harness_result.cost_usd if harness_result is not None else getattr(exc, "cost_usd", None),
        )
