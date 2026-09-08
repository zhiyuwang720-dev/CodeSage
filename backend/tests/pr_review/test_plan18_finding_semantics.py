from app.models.agent_task import AgentFinding
from app.contracts.final_review_contract import ReviewFinding
from app.control_plane.results import ReviewResultService


def _finding(category: str) -> ReviewFinding:
    return ReviewFinding(
        rule_id=f"RULE-{category}",
        severity="medium",
        category=category,
        title=f"{category} finding",
        description="deterministic evidence",
        file_path="src/a.py",
        line_start=10,
        line_end=10,
        confidence=0.9,
        needs_verification=False,
        verdict="confirmed",
        source="rules",
    )


def test_pr_rows_keep_category_and_versioned_payload_without_cve_gate():
    for category in ("security", "perf", "test_gap"):
        finding = _finding(category)
        row = ReviewResultService._finding_row("task-1", finding)
        assert row.category == category
        assert row.vulnerability_type is None
        assert row.finding_metadata["review_payload_version"] == 1
        assert row.finding_metadata["review_payload"]["category"] == category


def test_fingerprint_distinguishes_categories_at_same_location():
    rows = [ReviewResultService._finding_row("task-1", _finding(category)) for category in ("perf", "test_gap")]
    assert rows[0].fingerprint != rows[1].fingerprint


def test_historical_fingerprint_falls_back_to_legacy_type():
    row = AgentFinding(
        vulnerability_type="ssrf",
        severity="high",
        title="legacy",
        file_path="src/a.py",
        line_start=10,
    )
    assert row.generate_fingerprint()
