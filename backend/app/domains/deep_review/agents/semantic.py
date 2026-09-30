from __future__ import annotations

import asyncio
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.pipeline import Anatomy, SemanticBrief
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.prompt_loader import load_prompt, render_prompt
from app.domains.deep_review.services.runtime import AgentCallResult


BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class SemanticBriefDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative: str = Field(
        min_length=20,
        max_length=2000,
        description="Coherent description of what the PR does and where risk may arise.",
    )
    stated_intent: list[BoundedText] = Field(
        default_factory=list,
        max_length=20,
        description="Behavior promised by the PR title, description, or commit messages.",
    )
    implemented_intent: list[BoundedText] = Field(
        default_factory=list,
        max_length=20,
        description="Behavior actually visible in the diff.",
    )
    intent_gaps: list[BoundedText] = Field(
        default_factory=list,
        max_length=20,
        description="Promised behavior that is absent or incomplete in the diff.",
    )
    unrelated_changes: list[BoundedText] = Field(
        default_factory=list,
        max_length=20,
        description="Diff behavior not explained by the stated PR intent.",
    )
    risk_surfaces: list[BoundedText] = Field(
        default_factory=list,
        max_length=20,
        description="Areas worth investigation; do not report conclusions or bugs.",
    )
    hypotheses: list[BoundedText] = Field(
        default_factory=list,
        max_length=20,
        description="Falsifiable questions for later review; do not state findings.",
    )
    confidence: float = Field(
        default=0.5,
        ge=0,
        le=1,
        description="Overall confidence in semantic understanding.",
    )


def _safe_error(exc: BaseException) -> str:
    # Provider errors may contain request headers or response bodies.
    return type(exc).__name__


def _compact_diff_summary(anatomy: Anatomy, config: DeepReviewConfig) -> dict:
    files: list[dict] = []
    budget = min(config.max_diff_bytes, 18_000)
    changes = sorted(anatomy.files, key=lambda item: item.path)
    patches = [change.diff.encode("utf-8") for change in changes]
    share = budget // max(1, len(changes))
    allocations = [min(len(patch), 1_500, share) for patch in patches]
    remaining = budget - sum(allocations)
    for index, patch in enumerate(patches):
        extra = min(remaining, len(patch) - allocations[index], 1_500 - allocations[index])
        allocations[index] += extra
        remaining -= extra
    for change, encoded, limit in zip(changes, patches, allocations):
        patch = encoded[:limit].decode("utf-8", "ignore")
        truncated = len(encoded) > limit
        files.append(
            {
                "path": change.path,
                "change_type": change.change_type.value,
                "additions": change.additions,
                "deletions": change.deletions,
                "patch": patch,
                "patch_truncated": truncated,
            }
        )
    return {
        "stats": anatomy.stats.model_dump(),
        "directories": anatomy.directories,
        "clusters": [{"name": item.name, "files": item.files} for item in anatomy.clusters],
        "related_paths": anatomy.related_paths,
        "files": files,
    }


def build_semantic_prompt(
    snapshot: ReviewSnapshot, anatomy: Anatomy, config: DeepReviewConfig
) -> str:
    diff_summary = _compact_diff_summary(anatomy, config)
    payload = {
        "pr_metadata": {
            "title": snapshot.input.title[:300],
            "description": snapshot.input.description[:2_000],
            "commit_messages": [message[:200] for message in snapshot.commit_messages[:20]],
        },
        "diff_summary": diff_summary,
        "evidence_scope": {
            "note": (
                "You have no tools and cannot read the repository. Only the supplied "
                "metadata, anatomy summary, and bounded patches are visible. Do not "
                "assert behavior not observable from this evidence."
            ),
            "patch_truncated_files": [
                item["path"] for item in diff_summary["files"] if item["patch_truncated"]
            ],
        },
    }
    return render_prompt(
        "semantic_user",
        context_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _fallback_brief(snapshot: ReviewSnapshot, anatomy: Anatomy) -> SemanticBrief:
    title = snapshot.input.title.strip() or ", ".join(snapshot.commit_messages[:3])
    description = snapshot.input.description.strip()[:300]
    narrative = (
        f"Semantic model unavailable. PR title/commits: {title[:300]}. "
        f"Description: {description}. "
        f"Changed files: {anatomy.stats.total_files}; "
        f"+{anatomy.stats.total_additions}/-{anatomy.stats.total_deletions}; "
        f"directories: {', '.join(anatomy.directories[:10]) or 'none'}."
    )
    return SemanticBrief(source="fallback", narrative=narrative, confidence=0)


async def run_semantic_agent(
    runtime,
    *,
    snapshot: ReviewSnapshot,
    anatomy: Anatomy,
    config: DeepReviewConfig,
) -> AgentCallResult[SemanticBrief]:
    ai_result = None
    try:
        ai_result = await runtime.ai(
            build_semantic_prompt(snapshot, anatomy, config),
            system=load_prompt("semantic"),
            schema=SemanticBriefDraft,
            response_format="json",
        )
        draft = SemanticBriefDraft.model_validate(ai_result.parsed)
        brief = SemanticBrief(
            source="model",
            narrative=draft.narrative.strip(),
            stated_intent=list(draft.stated_intent),
            implemented_intent=list(draft.implemented_intent),
            intent_gaps=list(draft.intent_gaps),
            unrelated_changes=list(draft.unrelated_changes),
            risk_surfaces=list(draft.risk_surfaces),
            hypotheses=list(draft.hypotheses),
            confidence=draft.confidence,
        )
        return AgentCallResult(
            value=brief,
            session_id=None,
            usage=ai_result.usage,
            cost_usd=ai_result.cost_usd,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        usage = ai_result.usage if ai_result is not None else getattr(exc, "usage", None)
        cost_usd = ai_result.cost_usd if ai_result is not None else getattr(exc, "cost_usd", None)
        return AgentCallResult(
            value=_fallback_brief(snapshot, anatomy),
            error=_safe_error(exc),
            session_id=None,
            usage=usage,
            cost_usd=cost_usd,
        )
