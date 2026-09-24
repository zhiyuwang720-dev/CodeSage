"""Deterministic boundary between one Cross draft and domain results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.domains.deep_review.schemas.input import ChangeType
from app.domains.deep_review.schemas.pipeline import (
    CrossAnalysisResult, FindingDecision, ReviewFinding,
)
from app.domains.deep_review.services.directory_filter import FilterError, normalize_path
from app.domains.deep_review.services.input_builder import ReviewSnapshot


@dataclass(slots=True)
class CrossRepairOutcome:
    result: CrossAnalysisResult
    diagnostics: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.diagnostics)


def keep_all_result(candidates: list[ReviewFinding], reason: str) -> CrossAnalysisResult:
    return CrossAnalysisResult(
        decisions=[
            FindingDecision(finding_index=index, result="keep", reason=reason)
            for index in range(len(candidates))
        ],
        new_findings=[],
        unresolved_risks=[reason],
        summary="Cross Analysis could not finish; original Reviewer candidates were retained.",
    )


def repair_cross_result(
    payload: dict[str, Any],
    *,
    candidates: list[ReviewFinding],
    snapshot: ReviewSnapshot,
    head_line_counts: dict[str, int | None],
) -> CrossRepairOutcome:
    """Keep first legal index, conservatively fill gaps and validate new locations."""
    decisions_by_index: dict[int, FindingDecision] = {}
    diagnostics: list[str] = []
    for position, raw in enumerate(payload.get("decisions") or []):
        index = raw["finding_index"]
        if index >= len(candidates):
            diagnostics.append(f"cross_decision_out_of_range:{position}:{index}")
            continue
        if index in decisions_by_index:
            diagnostics.append(f"cross_decision_duplicate:{position}:{index}")
            continue
        result = raw["result"]
        reason = raw.get("reason", "").strip()
        if result == "drop" and not reason:
            result = "keep"
            diagnostics.append(f"cross_drop_without_reason:{index}")
            reason = "Drop reason was empty; retained by safety fallback."
        decisions_by_index[index] = FindingDecision(
            finding_index=index,
            result=result,
            reason=reason,
            revised_severity=raw.get("revised_severity"),
        )

    decisions: list[FindingDecision] = []
    for index in range(len(candidates)):
        if index not in decisions_by_index:
            diagnostics.append(f"cross_decision_missing:{index}")
            decisions.append(FindingDecision(
                finding_index=index, result="keep",
                reason="No valid Cross decision; retained by safety fallback.",
            ))
        else:
            decisions.append(decisions_by_index[index])

    deleted_paths = {
        change.path for change in snapshot.changes if change.change_type is ChangeType.DELETED
    }
    review_paths = set(snapshot.review_paths)
    new_findings: list[ReviewFinding] = []
    for position, raw in enumerate(payload.get("new_findings") or []):
        try:
            path = normalize_path(raw["file_path"])
        except (KeyError, FilterError, TypeError, ValueError):
            diagnostics.append(f"cross_new_finding_invalid_path:{position}")
            continue
        if path not in review_paths or path in deleted_paths:
            diagnostics.append(f"cross_new_finding_outside_review_paths:{position}")
            continue
        start, end = raw.get("line_start"), raw.get("line_end")
        if start is None and end is not None:
            diagnostics.append(f"cross_new_finding_end_without_start:{position}")
            continue
        if start is not None and end is None:
            end = start
        line_count = head_line_counts.get(path)
        if line_count is None or (start is not None and (end < start or end > line_count)):
            diagnostics.append(f"cross_new_finding_invalid_head_line:{position}")
            continue
        try:
            new_findings.append(ReviewFinding(
                **{**raw, "file_path": path, "line_end": end},
                source="cross",
                dimension_name="cross-analysis",
            ))
        except (TypeError, ValidationError):
            diagnostics.append(f"cross_new_finding_invalid_business_payload:{position}")

    result = CrossAnalysisResult(
        decisions=decisions,
        new_findings=new_findings,
        unresolved_risks=[str(item) for item in payload.get("unresolved_risks") or []],
        summary=str(payload.get("summary") or ""),
    )
    return CrossRepairOutcome(result=result, diagnostics=diagnostics)
