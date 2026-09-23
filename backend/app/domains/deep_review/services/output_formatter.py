from __future__ import annotations

import json
from typing import Any

from app.domains.deep_review.schemas.pipeline import ReviewDimension, ReviewFinding, SemanticBrief
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
    dimension_payload = {
        "name": dimension.name,
        "review_prompt": dimension.review_prompt,
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
