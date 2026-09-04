"""spec §6 test_synthesizer_priority: 严重度合并取最高; 初始只出 critical/high; 条数上限生效。"""
from app.services.pr_review.synthesizer import finding_to_comment, merge_dedup, rank_and_limit, synthesize
from app.services.contracts.final_review_contract import ReviewFinding


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
    assert "quality" in merged[0].source and "security" in merged[0].source


def test_same_severity_uses_confidence():
    merged, _ = merge_dedup([_model(_finding("security", "high", confidence=0.6)), _model(_finding("architecture", "high", confidence=0.95))])
    assert "architecture" in merged[0].source and "security" in merged[0].source
    assert merged[0].confidence == 0.95, "同严重度取高置信度"


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
