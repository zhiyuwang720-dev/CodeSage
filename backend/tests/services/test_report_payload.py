"""08-P2: build_report_payload 结构测试(PR 基本信息块 + duration_ms + finding_type 归一化)。

build_report_payload 忽略 db 参数(仅拼装), 测试传 None 即可。
"""
from datetime import datetime, timezone

import pytest

from app.models.agent_task import AgentFinding, AgentTask
from app.models.project import Project
from app.services.task_report_service import build_report_payload


def _task(**overrides) -> AgentTask:
    base = dict(
        id="task-1",
        status="completed",
        current_phase="done",
        name="PR audit",
        security_score=90.0,
        analyzed_files=4,
        total_iterations=7,
        tool_calls_count=15,
        tokens_used=4321,
        max_iterations=50,
        token_budget=100000,
        false_positive_count=1,
        branch_name="feat/x",
        commit_sha="base000",
        started_at=datetime(2026, 5, 19, 8, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 5, 19, 8, 0, 3, tzinfo=timezone.utc),
        agent_config={},
    )
    base.update(overrides)
    return AgentTask(**base)


def _finding(**overrides) -> AgentFinding:
    base = dict(
        id="f-1",
        task_id="task-1",
        vulnerability_type="ssrf",
        severity="high",
        title="SSRF",
        description="x",
        file_path="a.py",
        line_start=1,
        status="new",
        is_verified=True,
    )
    base.update(overrides)
    return AgentFinding(**base)


def _project() -> Project:
    return Project(id="proj-1", name="demo", source_type="repository", repository_url="https://github.com/x/demo")


@pytest.mark.asyncio
async def test_build_report_payload_pr_block_from_pr_meta():
    task = _task(
        agent_config={
            "pr_meta": {
                "pr_url": "https://github.com/x/demo/pull/7",
                "pr_number": 7,
                "title": "Fix auth",
                "branch": "feat/x",
                "base_sha": "base000",
                "head_sha": "head111",
                "author": "alice",
            }
        }
    )
    payload = await build_report_payload(None, task, _project(), [_finding()])

    assert payload.report["type"] == "audit_report"
    assert payload.pr.pr_url == "https://github.com/x/demo/pull/7"
    assert payload.pr.pr_number == 7
    assert payload.pr.title == "Fix auth"
    assert payload.pr.head_sha == "head111"
    assert payload.pr.author == "alice"
    # 无 pr_meta 时兜底 task 列
    assert payload.pr.branch == "feat/x"
    assert payload.pr.base_sha == "base000"


@pytest.mark.asyncio
async def test_build_report_payload_pr_block_fallback_without_pr_meta():
    task = _task()  # agent_config={}
    payload = await build_report_payload(None, task, _project(), [])

    assert payload.pr.pr_url is None
    assert payload.pr.pr_number is None
    # 无 pr_meta 时仍从 task 列兜底
    assert payload.pr.branch == "feat/x"
    assert payload.pr.base_sha == "base000"
    assert payload.pr.head_sha is None


@pytest.mark.asyncio
async def test_build_report_payload_duration_ms():
    task = _task()
    payload = await build_report_payload(None, task, _project(), [])

    # started_at→completed_at 3 秒
    assert payload.summary.duration_ms == 3000
    assert payload.summary.total_iterations == 7
    assert payload.summary.tool_calls_count == 15
    assert payload.summary.tokens_used == 4321
    assert payload.summary.max_iterations == 50
    assert payload.summary.token_budget == 100000


@pytest.mark.asyncio
async def test_build_report_payload_duration_ms_none_when_unfinished():
    task = _task(started_at=None, completed_at=None)
    payload = await build_report_payload(None, task, _project(), [])
    assert payload.summary.duration_ms is None


@pytest.mark.asyncio
async def test_build_report_payload_finding_type_normalized():
    """raw dict 仍带 vulnerability_type 键时归一化为 finding_type(过渡期兼容)。"""
    task = _task()
    findings = [
        {"vulnerability_type": "idor", "severity": "high", "title": "IDOR", "file_path": "a.py", "is_verified": False}
    ]
    payload = await build_report_payload(None, task, _project(), findings)

    assert payload.findings[0].finding_type == "idor"
    assert payload.summary.total_findings == 1
