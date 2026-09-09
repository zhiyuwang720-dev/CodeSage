"""spec §6 test_synthesizer_priority: 严重度合并取最高; 初始只出 critical/high; 条数上限生效。"""
import pytest
from pydantic import ValidationError

from app.domains.pr_review.synthesizer import finding_to_comment, merge_dedup, rank_and_limit, synthesize
from app.contracts.final_review_contract import ReviewFinding
from app.contracts.review_execution import StageResult


def _finding(source: str, severity: str, line: int = 3, confidence: float = 0.8, category: str = "security") -> dict:
    return dict(
        rule_id=f"{source}-R", severity=severity, category=category,
        title="t", description="d", file_path="a.py", line_start=line, line_end=line,
        confidence=confidence, needs_verification=False, verdict="confirmed", source=source,
    )


def _model(finding: dict) -> ReviewFinding:
    return ReviewFinding.model_validate(finding)


def test_severity_conflict_takes_highest():
    merged, deduped = merge_dedup([_model(_finding("quality", "low")), _model(_finding("security", "critical"))])
    assert deduped == 1
    assert merged[0].severity == "critical"
    assert merged[0].source == "security"
    assert merged[0].contributing_sources == ["security", "quality"]


def test_same_severity_uses_confidence():
    merged, _ = merge_dedup([_model(_finding("security", "high", confidence=0.6)), _model(_finding("architecture", "high", confidence=0.95))])
    assert merged[0].source == "architecture"
    assert merged[0].contributing_sources == ["security", "architecture"]
    assert merged[0].confidence == 0.95, "同严重度取高置信度"


def test_multi_source_merge_is_order_stable_and_stage_result_valid():
    architecture = _model(_finding("architecture", "high", confidence=0.8))
    quality = _model(_finding("quality", "high", confidence=0.8))
    first, _ = merge_dedup([architecture, quality])
    reversed_result, _ = merge_dedup([quality, architecture])

    assert first[0].model_dump(mode="json") == reversed_result[0].model_dump(mode="json")
    assert first[0].source == "architecture"
    assert first[0].contributing_sources == ["architecture", "quality"]

    stage = StageResult.model_validate(
        {
            "run_id": "run-1",
            "stage_type": "report",
            "status": "completed",
            "findings": [first[0].model_dump(mode="json")],
        }
    )
    restored = StageResult.model_validate_json(stage.model_dump_json())
    assert restored.findings[0].contributing_sources == ["architecture", "quality"]
    assert finding_to_comment(restored.findings[0])["body"].startswith(
        "[Architecture + Quality]"
    )


def test_single_source_and_legacy_payload_fill_contributing_sources():
    finding = _model(_finding("rules", "medium"))
    assert finding.contributing_sources == ["rules"]

    payload = finding.model_dump(mode="json", exclude={"contributing_sources"})
    restored = ReviewFinding.model_validate(payload)
    assert restored.contributing_sources == ["rules"]


def test_contributing_sources_are_deduplicated_sorted_and_validated():
    payload = _finding("architecture", "high")
    payload["contributing_sources"] = ["quality", "security", "quality"]
    finding = ReviewFinding.model_validate(payload)
    assert finding.contributing_sources == ["security", "architecture", "quality"]

    payload["contributing_sources"] = ["unknown"]
    with pytest.raises(ValidationError, match="contributing_sources"):
        ReviewFinding.model_validate(payload)


def test_initial_only_critical_and_high():
    findings = [
        _finding("security", "critical"),
        _finding("architecture", "high"),
        _finding("quality", "medium"),
        _finding("rules", "low"),
    ]
    out = rank_and_limit([_model(f) for f in findings])
    assert {f.severity for f in out} == {"critical", "high"}, "低噪原则: medium/low 不出"


def test_max_comments_limit():
    findings = [_finding("security", "critical", line=line) for line in range(2, 20)]
    out = rank_and_limit([_model(f) for f in findings], max_comments=5)
    assert len(out) == 5


def test_synthesize_end_to_end_with_off_diff_rejection():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n import os\n+import json\n"
    raw = [_finding("security", "critical", line=2), _finding("quality", "high", line=99)]
    result = synthesize(raw, diff_text=diff)
    assert result.rejected_off_diff == 1, "非新增行被拒"
    assert len(result.comments) == 1


def test_severity_dropped_counts_filtered():
    """min_severity=high 时 medium/low 计入 severity_dropped(空结果自解释的依据)。"""
    findings = [
        _finding("security", "critical", line=3),
        _finding("architecture", "high", line=5),
        _finding("quality", "medium", line=7),
        _finding("rules", "low", line=9),
    ]
    result = synthesize(findings, min_severity="high")
    assert result.severity_dropped == 2, "medium/low 各 1 条被严重度过滤"
    assert {f.severity for f in result.comments} == {"critical", "high"}


def test_severity_dropped_zero_when_low():
    """min_severity=low(全量输出)时无严重度过滤。"""
    findings = [
        _finding("security", "critical", line=3),
        _finding("quality", "medium", line=7),
        _finding("rules", "low", line=9),
    ]
    result = synthesize(findings, min_severity="low")
    assert result.severity_dropped == 0
    assert len(result.comments) == 3


def test_finding_to_comment_benchmark_shape():
    comment = finding_to_comment(_model(_finding("security", "high")))
    assert set(comment) >= {"path", "line", "body", "severity", "category"}
    assert comment["path"] == "a.py" and comment["severity"] == "high"
