from datetime import datetime, timezone

from app.models.agent_task import AgentFinding
from app.services.task_report_service import DEFAULT_REPORT_TEMPLATE, render_report_content, serialize_finding


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


def test_serialize_finding_uses_finding_type_audit_key():
    """08-P2: serialize_finding 输出审计语义键 finding_type, 不再暴露 vulnerability_type。"""
    finding = AgentFinding(
        id="finding-2",
        task_id="task-1",
        vulnerability_type="ssrf",
        severity="high",
        title="SSRF",
        file_path="a.py",
        status="new",
        is_verified=False,
    )
    payload = serialize_finding(finding)

    assert payload["finding_type"] == "ssrf"
    assert "vulnerability_type" not in payload


def test_render_report_default_audit_wording_and_pr_block():
    """08-P2: 默认模板为审计语义, 渲染含 PR 基本信息块 + 运行统计参数。"""
    payload = {
        "pr": {
            "pr_url": "https://github.com/x/y/pull/7",
            "pr_number": 7,
            "title": "Fix auth",
            "branch": "fix-auth",
            "base_sha": "abc123",
            "head_sha": None,
            "author": "alice",
        },
        "project": {"name": "demo"},
        "task": {"id": "t-1", "name": "t", "status": "completed", "phase": None},
        "report": {"generated_at": "2026-05-19T08:30:00+00:00"},
        "summary": {
            "security_score": 88.0,
            "total_findings": 1,
            "verified_findings": 1,
            "false_positive_count": 0,
            "total_files_analyzed": 3,
            "severity_distribution": {"critical": 0, "high": 1, "medium": 0, "low": 0},
            "origin_distribution": {"scan_triage": 0, "direct_finding": 1, "other": 0},
            "total_iterations": 5,
            "tool_calls_count": 12,
            "tokens_used": 3456,
            "max_iterations": 50,
            "token_budget": 100000,
            "duration_ms": 1200,
        },
        "findings": [
            {
                "finding_type": "ssrf",
                "severity": "high",
                "title": "SSRF in fetch",
                "description": "x",
                "file_path": "a.py",
                "line_start": 1,
            }
        ],
        "final_conclusions": [],
        "template": {"name": None},
    }
    rendered = render_report_content(payload, DEFAULT_REPORT_TEMPLATE)

    assert "# CodeSage PR 审计报告" in rendered
    assert "## PR 审计基本信息" in rendered
    assert "PR URL: https://github.com/x/y/pull/7" in rendered
    assert "PR 编号: 7" in rendered
    assert "标题: Fix auth" in rendered
    assert "base → head: abc123 → N/A" in rendered
    assert "作者: alice" in rendered
    assert "## 审计发现清单" in rendered
    assert "问题类型: ssrf" in rendered
    assert "已验证问题: 1" in rendered
    assert "误报数量: 0" in rendered
    # 运行统计参数
    assert "总迭代数: 5" in rendered
    assert "工具调用数: 12" in rendered
    assert "Token 用量: 3456" in rendered
    assert "最大迭代数: 50" in rendered
    assert "Token 预算: 100000" in rendered
    assert "总耗时(ms): 1200" in rendered
    # 无漏洞语义措辞残留
    assert "已验证漏洞" not in rendered
    assert "漏洞清单" not in rendered
    assert "漏洞类型" not in rendered


def test_render_report_json_format():
    """08-P2: JSON 格式导出为审计语义结构化 payload, 键为 finding_type。"""
    payload = {
        "pr": {"pr_url": None, "pr_number": None, "title": None, "branch": None, "base_sha": None, "head_sha": None, "author": None},
        "project": {"name": "demo"},
        "task": {"id": "t-1", "name": None, "status": "completed", "phase": None},
        "report": {"generated_at": "2026-05-19T08:30:00+00:00", "type": "audit_report"},
        "summary": {
            "total_findings": 1,
            "verified_findings": 0,
            "confirmed_findings": 0,
            "candidate_findings": 1,
            "false_positive_findings": 0,
            "severity_distribution": {},
            "origin_distribution": {},
            "total_iterations": 0,
            "tool_calls_count": 0,
            "tokens_used": 0,
        },
        "findings": [{"finding_type": "idor", "severity": "medium", "title": "IDOR", "description": None, "file_path": None}],
        "final_conclusions": [],
        "template": {"name": None},
    }
    rendered = render_report_content(payload, "", output_format="json")

    assert rendered.strip().startswith("{")
    assert '"finding_type": "idor"' in rendered
    assert '"vulnerability_type"' not in rendered
