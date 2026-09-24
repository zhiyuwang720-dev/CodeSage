from __future__ import annotations

import json
from typing import Any

from app.domains.deep_review.schemas.pipeline import (
    Anatomy, EvidencePackage, ReviewDimension, ReviewFinding, ReviewPlan, SemanticBrief,
)
from app.domains.deep_review.schemas.input import ChangeType
from app.domains.deep_review.services.diff_engine import find_hunk_for_new_line, parse_hunks
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
    """Send every claim once, with only accurately matched and deduplicated diff hunks."""
    # Semantic already informed Planner and Reviewers; Cross receives claims and
    # source evidence, not that fallible interpretation.
    del semantic
    review_paths = sorted(snapshot.review_paths)
    constraints = {
        "base_commit": snapshot.base_commit,
        "head_commit": snapshot.head_commit,
        "review_paths": review_paths,
        "max_turns": max_turns,
    }
    successful_paths = {
        path for item in reviewers if item.status == "succeeded"
        for path in item.target_files
    }
    gaps = {
        "incomplete_dimensions": [
            {"name": item.dimension_name, "status": item.status, "target_files": item.target_files}
            for item in reviewers if item.status != "succeeded"
        ],
        "paths_without_successful_reviewer": sorted(set(review_paths) - successful_paths),
    }
    changes = {change.path: change for change in snapshot.changes}
    hunks_by_path = {
        path: anatomy.hunks.get(path) or parse_hunks(change)
        for path, change in changes.items()
    }
    claims: list[dict[str, Any]] = []
    matched_hunks: dict[tuple[str, str], dict[str, Any]] = {}
    for index, finding in enumerate(candidates):
        change = changes.get(finding.file_path)
        package = evidence.get(index, EvidencePackage(finding_index=index))
        matched = find_hunk_for_new_line(
            hunks_by_path.get(finding.file_path, []), finding.line_start,
        )
        if finding.file_path not in snapshot.review_paths:
            status = "outside_review_scope"
            explanation = "Candidate path is outside review_paths; do not publish it as a Finding."
        elif change is None or not change.diff:
            status = "no_text_diff"
            explanation = "No text patch was captured; do not infer that the claim is false."
        elif change.change_type is ChangeType.BINARY:
            status = "binary_diff"
            explanation = "Binary content cannot be verified from a text hunk."
        elif change.change_type is ChangeType.DELETED:
            status = "deleted_file"
            explanation = "No head-file content exists; inspect this path with file_read_diff, not file_read."
        elif finding.line_start is None:
            status = "no_line"
            explanation = "File-level or cross-file claim: locate the relevant change with file_read_diff and code_search; no hunk was guessed."
        elif matched is None:
            status = "outside_changed_hunks"
            explanation = "Reported head line is outside changed hunks; compare file_read_diff with file_read near the line."
        elif not package.diff_hunk or not matched.content.startswith(package.diff_hunk):
            status = "excerpt_unavailable"
            explanation = "A covering hunk exists but no matching bounded excerpt was retained; use file_read_diff."
        else:
            status = "matched_hunk"
            explanation = None
            key = (finding.file_path, package.diff_hunk)
            entry = matched_hunks.setdefault(key, {
                "path": finding.file_path,
                "candidate_indices": [],
                "hunk_header": matched.header,
                "diff_hunk": package.diff_hunk,
                "excerpt_truncated": len(package.diff_hunk.encode("utf-8")) < len(matched.content.encode("utf-8")),
            })
            entry["candidate_indices"].append(index)
        claim = {
            "index": index,
            "dimension_name": finding.dimension_name,
            "file_path": finding.file_path,
            "line_start": finding.line_start,
            "line_end": finding.line_end,
            "severity": finding.severity,
            "title": finding.title,
            "body": finding.body,
            "evidence": finding.evidence,
            "hunk_status": status,
        }
        if explanation is not None:
            claim["location_explanation"] = explanation
        claims.append(claim)
    return {
        "run_constraints_json": _stable_json(constraints),
        "cross_hints_json": _stable_json([item.model_dump(mode="json") for item in plan.cross_reference_hints]),
        "coverage_gaps_json": _stable_json(gaps),
        "candidate_claims_json": _stable_json(claims),
        "matched_hunks_json": _stable_json(list(matched_hunks.values())),
    }
