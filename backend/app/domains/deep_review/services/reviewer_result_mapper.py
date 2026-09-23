from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.domains.deep_review.schemas.input import ChangeType
from app.domains.deep_review.schemas.pipeline import ReviewDimension, ReviewFinding, ReviewerResult
from app.domains.deep_review.services.directory_filter import FilterError, normalize_path
from app.domains.deep_review.services.input_builder import ReviewSnapshot


@dataclass(slots=True)
class ReviewerMappingResult:
    result: ReviewerResult
    diagnostics: list[str] = field(default_factory=list)
    rejected_count: int = 0


def map_reviewer_result(
    payload: dict[str, Any],
    *,
    dimension: ReviewDimension,
    snapshot: ReviewSnapshot,
    head_line_counts: dict[str, int | None],
) -> ReviewerMappingResult:
    """Turn an Agent Draft payload into checked domain findings."""
    target_files = set(dimension.target_files)
    deleted_paths = {
        change.path for change in snapshot.changes if change.change_type is ChangeType.DELETED
    }
    mapped: list[ReviewFinding] = []
    diagnostics: list[str] = []
    rejected = 0

    for index, item in enumerate(payload.get("findings") or []):
        reason: str | None = None
        try:
            path = normalize_path(str(item.get("file_path") or ""))
        except (FilterError, TypeError, ValueError):
            path = ""
            reason = "invalid_path"

        if reason is None and path not in target_files:
            reason = "path_not_owned_by_dimension"
        elif reason is None and path in deleted_paths:
            reason = "deleted_path_has_no_head_location"

        start = item.get("line_start")
        end = item.get("line_end")
        if reason is None and start is None and end is not None:
            reason = "line_end_without_line_start"
        elif reason is None and start is not None and end is None:
            end = start
        elif reason is None and start is not None and end is not None and end < start:
            reason = "reversed_line_range"

        line_count = head_line_counts.get(path) if path else None
        if reason is None and line_count is None:
            reason = "head_file_unavailable_or_non_regular"
        elif reason is None and start is not None and (start < 1 or end is None or end > line_count):
            reason = "line_outside_head_file"

        if reason is not None:
            rejected += 1
            diagnostics.append(f"finding[{index}] rejected: {reason}")
            continue

        try:
            mapped.append(
                ReviewFinding(
                    file_path=path,
                    line_start=start,
                    line_end=end,
                    severity=item["severity"],
                    title=item["title"],
                    body=item["body"],
                    evidence=item.get("evidence", ""),
                    suggestion=item.get("suggestion", ""),
                    confidence=item.get("confidence", 0.5),
                    tags=item.get("tags", []),
                    dimension_name=dimension.name,
                    source="reviewer",
                )
            )
        except (KeyError, TypeError, ValidationError) as exc:
            rejected += 1
            diagnostics.append(f"finding[{index}] rejected: invalid_business_finding:{type(exc).__name__}")

    if rejected and not mapped and payload.get("findings"):
        diagnostics.append("all_findings_rejected: dimension degraded")

    try:
        result = ReviewerResult(findings=mapped, summary=str(payload.get("summary") or ""))
    except ValidationError as exc:
        # The Agent Draft already passed validation. This is a defensive boundary
        # if business constraints evolve independently from the Draft schema.
        diagnostics.append(f"reviewer_result_rejected:{type(exc).__name__}")
        rejected += len(mapped)
        result = ReviewerResult(findings=[], summary="")

    return ReviewerMappingResult(result=result, diagnostics=diagnostics, rejected_count=rejected)
