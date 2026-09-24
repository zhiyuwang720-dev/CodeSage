from __future__ import annotations

import json
from typing import Any

from app.domains.deep_review.schemas.pipeline import (
    Anatomy, EvidencePackage, ReviewDimension, ReviewFinding, ReviewPlan, SemanticBrief,
)
from app.domains.deep_review.schemas.output import ReviewerDimensionReport
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.prompt_loader import render_prompt


MAX_REVIEWER_DIFF_BYTES = 48_000
_TRUNCATION_MARKER = "\n[DIFF TRUNCATED: inspect the fixed head with file_read/file_read_diff.]"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _target_diffs(snapshot: ReviewSnapshot, target_files: list[str]) -> list[dict[str, Any]]:
    """Keep every target visible while distributing a bounded diff budget fairly."""
    paths = list(target_files)
    raw_patches = [snapshot.diff_by_path.get(path, "") for path in paths]
    encoded = [patch.encode("utf-8") for patch in raw_patches]
    if not paths:
        return []

    share = MAX_REVIEWER_DIFF_BYTES // len(paths)
    limits = [min(len(raw), share) for raw in encoded]
    remaining = MAX_REVIEWER_DIFF_BYTES - sum(limits)
    for index, raw in enumerate(encoded):
        extra = min(remaining, max(0, len(raw) - limits[index]))
        limits[index] += extra
        remaining -= extra

    changes = {change.path: change for change in snapshot.changes}
    result: list[dict[str, Any]] = []
    for path, patch, raw, limit in zip(paths, raw_patches, encoded, limits):
        truncated = len(raw) > limit
        excerpt = raw[:limit].decode("utf-8", errors="ignore")
        if truncated:
            excerpt += _TRUNCATION_MARKER
        change = changes.get(path)
        result.append(
            {
                "path": path,
                "change_type": change.change_type.value if change else "unknown",
                "additions": change.additions if change else 0,
                "deletions": change.deletions if change else 0,
                "diff_available": bool(patch),
                "diff_truncated": truncated,
                "diff": excerpt,
            }
        )
    return result


def build_reviewer_prompt(
    *,
    snapshot: ReviewSnapshot,
    dimension: ReviewDimension,
    semantic: SemanticBrief,
) -> str:
    if len(dimension.investigations) == 1:
        investigation_prompt = dimension.investigations[0].review_prompt
    else:
        investigation_prompt = "Investigations for this dimension (in priority order):\n" + "\n".join(
            f"{index}. [{item.source_dimension}] {item.review_prompt}"
            for index, item in enumerate(dimension.investigations, start=1)
        )
    dimension_payload = {
        "name": dimension.name,
        "review_prompt": investigation_prompt,
        "priority": dimension.priority,
        "fallback": dimension.fallback,
    }
    scope_payload = {
        "base_commit": snapshot.base_commit,
        "head_commit": snapshot.head_commit,
        "target_files": dimension.target_files,
        "context_files": dimension.context_files,
    }
    return render_prompt(
        "reviewer_user",
        dimension_json=_stable_json(dimension_payload),
        scope_json=_stable_json(scope_payload),
        semantic_json=_stable_json(semantic.model_dump(mode="json")),
        target_diffs_json=_stable_json(_target_diffs(snapshot, dimension.target_files)),
    )


def format_candidate_summaries(findings: list[ReviewFinding]) -> str:
    """Format all ordered Candidates for a later Cross call without mutating them."""
    rows: list[dict[str, Any]] = []
    for index, finding in enumerate(findings):
        body = finding.body
        evidence = finding.evidence
        body_limit, evidence_limit = 2_000, 1_500
        rows.append(
            {
                "index": index,
                "dimension_name": finding.dimension_name,
                "file_path": finding.file_path,
                "line_start": finding.line_start,
                "line_end": finding.line_end,
                "severity": finding.severity,
                "title": finding.title,
                "body": body[:body_limit],
                "body_truncated": len(body) > body_limit,
                "evidence": evidence[:evidence_limit],
                "evidence_truncated": len(evidence) > evidence_limit,
            }
        )
    return _stable_json(rows)


def format_cross_inputs(
    *,
    snapshot: ReviewSnapshot,
    anatomy: Anatomy,
    semantic: SemanticBrief,
    plan: ReviewPlan,
    reviewers: list[ReviewerDimensionReport],
    candidates: list[ReviewFinding],
    evidence: dict[int, EvidencePackage],
    max_turns: int,
) -> dict[str, str]:
    """One stable JSON block per trust boundary; preserve every candidate index."""
    review_paths = sorted(snapshot.review_paths)
    context_paths = sorted(snapshot.context_paths)
    active_targets = {
        path for item in reviewers if item.status in {"succeeded", "degraded"}
        for path in item.target_files
    }
    constraints = {
        "base_commit": snapshot.base_commit,
        "head_commit": snapshot.head_commit,
        "review_paths": review_paths,
        "context_paths": context_paths,
        "snapshot_rule": "read fixed head only; new finding primary path must be in review_paths",
        "max_turns": max_turns,
    }
    internal_hints = [
        {"dimension_name": dimension.name, "review_prompt": investigation.review_prompt}
        for dimension in plan.dimensions if not dimension.deferred
        for investigation in dimension.investigations
        if investigation.source_dimension == "cross_reference_hint"
    ]
    relations = {
        "cross_reference_hints": [item.model_dump(mode="json") for item in plan.cross_reference_hints],
        "internalized_hints_already_assigned_to_reviewer": internal_hints,
        "dimension_name_map": plan.dimension_name_map,
        "anatomy_related_paths": anatomy.related_paths,
        "plan_unresolved_risks": plan.unresolved_risks,
    }
    coverage = {
        "dimensions": [
            {
                "name": item.dimension_name,
                "status": item.status,
                "target_files": item.target_files,
                "context_files": item.context_files,
                "finding_count": item.finding_count,
            }
            for item in reviewers
        ],
        "paths_without_successful_reviewer": sorted(set(review_paths) - active_targets),
        "coverage_complete": plan.coverage_complete
        and all(item.status == "succeeded" for item in reviewers),
    }
    summaries = [
        {"index": index, **finding.model_dump(mode="json")}
        for index, finding in enumerate(candidates)
    ]
    packages = []
    for index in range(len(candidates)):
        package = evidence.get(index, EvidencePackage(finding_index=index))
        packages.append({
            "index": index,
            "evidence_empty": not any((
                package.primary_code, package.diff_hunk, package.caller_snippets,
                package.cross_ref_snippets, package.related_code,
            )),
            "truncated": package.truncated,
            "package": package.model_dump(mode="json"),
        })
    return {
        "run_constraints_json": _stable_json(constraints),
        "semantic_json": _stable_json(semantic.model_dump(mode="json")),
        "plan_relations_json": _stable_json(relations),
        "reviewer_coverage_json": _stable_json(coverage),
        "candidate_summaries_json": _stable_json(summaries),
        "evidence_packages_json": _stable_json(packages),
    }
