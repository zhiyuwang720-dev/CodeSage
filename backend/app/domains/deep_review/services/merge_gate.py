"""Pure final projection: Cross decisions, exact dedup, severity gate, order.

PR-AF 48ae7eeb (src/pr_af/merge_gate.py) motivated a final acceptance
boundary. CodeSage V1 uses deterministic code here; it does not port the
per-finding model gate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.pipeline import (
    CrossAnalysisResult, EvidencePackage, ReviewFinding,
)
from app.domains.deep_review.services.diff_engine import parse_hunks
from app.domains.deep_review.services.input_builder import ReviewSnapshot
from app.domains.deep_review.services.polish import polish_finding
from app.domains.deep_review.services.scoring import FindingScore, SEVERITY_ORDER, score_finding


@dataclass(slots=True)
class MergeOutcome:
    findings: list[ReviewFinding]
    scores: list[FindingScore]
    dropped_by_cross: int = 0
    exact_duplicates: int = 0
    filtered: int = 0
    diagnostics: list[str] = field(default_factory=list)


def _canonical(finding: ReviewFinding) -> str:
    return json.dumps(finding.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _exact_key(finding: ReviewFinding) -> tuple:
    return (
        finding.file_path.casefold(), finding.line_start, finding.line_end,
        _normalized_text(finding.title), _normalized_text(finding.body),
    )


def _winner_key(item: tuple[ReviewFinding, FindingScore]) -> tuple:
    finding, score = item
    return (
        -SEVERITY_ORDER[finding.severity], finding.confidence,
        score.evidence_completeness, score.diff_proximity, _canonical(finding),
    )


def merge_findings(
    candidates: list[ReviewFinding],
    cross: CrossAnalysisResult,
    evidence: dict[int, EvidencePackage],
    snapshot: ReviewSnapshot,
    config: DeepReviewConfig,
) -> MergeOutcome:
    """Input candidates and their evidence share the original stable index."""
    hunks_by_path = {change.path: parse_hunks(change) for change in snapshot.changes}
    decision_by_index = {item.finding_index: item for item in cross.decisions}
    scored: list[tuple[ReviewFinding, FindingScore]] = []
    dropped = 0
    for index, candidate in enumerate(candidates):
        decision = decision_by_index.get(index)
        if decision is not None and decision.result == "drop":
            dropped += 1
            continue
        finding = candidate
        if decision is not None and decision.revised_severity is not None:
            finding = ReviewFinding.model_validate({
                **candidate.model_dump(mode="python"),
                "severity": decision.revised_severity,
            })
        finding = polish_finding(finding)
        scored.append((finding, score_finding(
            finding, evidence=evidence.get(index),
            head_hunks=hunks_by_path.get(finding.file_path),
        )))
    for candidate in cross.new_findings:
        finding = polish_finding(candidate)
        scored.append((finding, score_finding(
            finding, head_hunks=hunks_by_path.get(finding.file_path),
        )))

    eligible = [item for item in scored if item[0].title and item[0].body.strip()
                and SEVERITY_ORDER[item[0].severity] <= SEVERITY_ORDER[config.min_severity]]
    by_key: dict[tuple, tuple[ReviewFinding, FindingScore]] = {}
    for item in eligible:
        key = _exact_key(item[0])
        if key not in by_key or _winner_key(item) > _winner_key(by_key[key]):
            by_key[key] = item
    ordered = sorted(
        by_key.values(),
        key=lambda item: (
            SEVERITY_ORDER[item[0].severity], item[0].file_path,
            item[0].line_start or 0, item[0].title.casefold(), _canonical(item[0]),
        ),
    )
    diagnostics: list[str] = []
    if config.max_final_findings is not None and len(ordered) > config.max_final_findings:
        diagnostics.append(f"max_final_findings_truncated:{len(ordered) - config.max_final_findings}")
        ordered = ordered[:config.max_final_findings]
    return MergeOutcome(
        findings=[item[0] for item in ordered],
        scores=[item[1] for item in ordered],
        dropped_by_cross=dropped,
        exact_duplicates=len(eligible) - len(by_key),
        filtered=len(scored) - len(eligible),
        diagnostics=diagnostics,
    )


def stable_business_hash(
    findings: list[ReviewFinding], summary: str, unresolved_risks: list[str], status: str,
) -> str:
    payload = {
        "status": status,
        "findings": [item.model_dump(mode="json") for item in findings],
        "summary": summary,
        "unresolved_risks": unresolved_risks,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
