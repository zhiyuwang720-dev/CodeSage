"""Deterministic quality records for the CodeSageDeep prototype.

PR-AF 48ae7eeb (src/pr_af/scoring.py) supplies the severity × confidence
idea. Evidence completeness and diff proximity are CodeSage tie breakers,
not copied PR-AF multipliers.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domains.deep_review.schemas.pipeline import DiffHunk, EvidencePackage, ReviewFinding


SEVERITY_WEIGHT = {"critical": 1.0, "high": 0.8, "medium": 0.5, "low": 0.2}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(frozen=True, slots=True)
class FindingScore:
    score: float
    evidence_completeness: int
    diff_proximity: int


def score_finding(
    finding: ReviewFinding,
    *,
    evidence: EvidencePackage | None = None,
    head_hunks: list[DiffHunk] | None = None,
) -> FindingScore:
    completeness = int(bool(evidence and evidence.primary_code)) + int(
        bool(evidence and evidence.diff_hunk)
    )
    proximity = 0
    if finding.line_start is not None and head_hunks:
        end = finding.line_end or finding.line_start
        proximity = int(any(
            hunk.new_start <= end
            and finding.line_start <= hunk.new_start + max(0, hunk.new_count - 1)
            for hunk in head_hunks
        ))
    return FindingScore(
        score=round(SEVERITY_WEIGHT[finding.severity] * finding.confidence, 3),
        evidence_completeness=completeness,
        diff_proximity=proximity,
    )
