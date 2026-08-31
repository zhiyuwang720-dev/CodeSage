from datetime import datetime, timezone

from app.models.agent_task import AgentFinding
from app.services.task_report_service import serialize_finding


def test_serialize_finding_includes_navigation_fields():
    finding = AgentFinding(
        id="finding-1",
        task_id="task-1",
        vulnerability_type="idor",
        severity="high",
        title="IDOR finding",
        description="Tenant check is missing.",
        file_path="server/api.py",
        line_start=42,
        status="new",
        is_verified=False,
    )
    finding.created_at = datetime(2026, 5, 19, 8, 30, tzinfo=timezone.utc)

    payload = serialize_finding(finding)

    assert payload["task_id"] == "task-1"
    assert payload["created_at"] == "2026-05-19T08:30:00+00:00"
