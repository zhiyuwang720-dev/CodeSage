import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from jinja2 import Template
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_task import AgentFinding, AgentTask
from app.models.project import Project
from app.models.report_template import AgentTaskReport
from app.services.contracts.final_finding_contract import filter_meaningful_exploit_chain, has_meaningful_poc
from app.services.contracts.report import ReportPayload, ReportPRInfo, ReportSummary, ReportFindingItem
from app.services.report_template_file_service import ReportTemplateFileService

DEFAULT_REPORT_TEMPLATE = """# CodeSage PR 审计报告

## PR 审计基本信息
{% if pr.pr_url or pr.pr_number %}
- PR URL: {{ pr.pr_url or 'N/A' }}
- PR 编号: {{ pr.pr_number if pr.pr_number is not none else 'N/A' }}
{% endif %}
- 标题: {{ pr.title or 'N/A' }}
- 仓库: {{ project.name }}
- 分支: {{ pr.branch or 'N/A' }}
- base → head: {{ pr.base_sha or 'N/A' }} → {{ pr.head_sha or 'N/A' }}
- 作者: {{ pr.author or 'N/A' }}

## 基本信息
- 生成时间: {{ report.generated_at }}
- 项目名称: {{ project.name }}
- 任务名称: {{ task.name or '未命名任务' }}
- 任务 ID: {{ task.id }}
- 当前状态: {{ task.status }}
- 使用模板: {{ template.name if template else '系统默认模板' }}

## 审计流程
- 编排模式: 固定 DAG / 显式状态机
- 执行链路: Orchestrator -> Recon -> (Scan -> Triage || Finding) -> Verification

## 执行摘要
- 安全评分: {{ summary.security_score if summary.security_score is not none else 'N/A' }}
- 发现总数: {{ summary.total_findings }}
- 已验证问题: {{ summary.verified_findings }}
- 误报数量: {{ summary.false_positive_count }}
- 分析文件数: {{ summary.total_files_analyzed }}

## 严重等级分布
- Critical: {{ summary.severity_distribution.critical }}
- High: {{ summary.severity_distribution.high }}
- Medium: {{ summary.severity_distribution.medium }}
- Low: {{ summary.severity_distribution.low }}

## 来源分布
- Scan/Triage: {{ summary.origin_distribution.scan_triage }}
- Direct Finding: {{ summary.origin_distribution.direct_finding }}
- Other: {{ summary.origin_distribution.other }}

## 运行统计
- 总迭代数: {{ summary.total_iterations }}
- 工具调用数: {{ summary.tool_calls_count }}
- Token 用量: {{ summary.tokens_used }}
- 缓存命中率: {{ (summary.cache_hit_ratio * 100)|round(1) if summary.cache_hit_ratio is not none else 'N/A' }}%
- 最大迭代数: {{ summary.max_iterations if summary.max_iterations is not none else 'N/A' }}
- Token 预算: {{ summary.token_budget if summary.token_budget is not none else 'N/A' }}
- 总耗时(ms): {{ summary.duration_ms if summary.duration_ms is not none else 'N/A' }}

## 审计发现清单
{% if findings %}
{% for finding in findings %}
### {{ loop.index }}. [{{ finding.severity|upper }}] {{ finding.title }}
- 问题类型: {{ finding.finding_type }}
- 来源: {{ finding.origin or 'unknown' }}
- 证据类型: {{ finding.evidence_type or 'unknown' }}
- 位置: {{ finding.file_path or 'N/A' }}{% if finding.line_start %}:{{ finding.line_start }}{% endif %}{% if finding.line_end and finding.line_end != finding.line_start %}-{{ finding.line_end }}{% endif %}
- 置信度: {{ finding.confidence if finding.confidence is not none else 'N/A' }}
- 是否验证: {{ '是' if finding.is_verified else '否' }}
- 描述: {{ finding.description or '无' }}
{% if finding.source %}- Source: {{ finding.source }}
{% endif %}{% if finding.sink %}- Sink: {{ finding.sink }}
{% endif %}{% if finding.code_snippet %}- 代码片段:
```text
{{ finding.code_snippet }}
```
{% endif %}{% if finding.suggestion %}- 修复建议: {{ finding.suggestion }}
{% endif %}{% if finding.poc_code %}- PoC:
```text
{{ finding.poc_code }}
```
{% endif %}
{% endfor %}
{% else %}
本次任务未产生可输出的审计发现。
{% endif %}

## 修复优先级建议
1. 优先修复 Critical / High 严重等级问题。
2. 结合 Scan/Triage 与 Finding 两条线索统一排期。
3. 对已确认发现优先补充修复与回归验证。
"""


def _severity_counts(findings: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for finding in findings:
        severity = str(finding.get("severity", "")).lower()
        if severity in counts:
            counts[severity] += 1
    return counts


def _origin_counts(findings: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"scan_triage": 0, "direct_finding": 0, "other": 0}
    for finding in findings:
        origin = str(finding.get("origin") or "other").lower()
        if origin not in counts:
            origin = "other"
        counts[origin] += 1
    return counts


def _report_status_counts(findings: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"confirmed": 0, "candidate": 0, "false_positive": 0}
    for finding in findings:
        report_status = str(finding.get("report_status") or finding.get("verdict") or "").lower()
        if report_status == "likely":
            report_status = "candidate"
        if report_status not in counts:
            report_status = "candidate"
        counts[report_status] += 1
    return counts


def serialize_finding(finding: AgentFinding | Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(finding, dict):
        item = dict(finding)
        item.setdefault("report_status", str(item.get("verdict") or "candidate").lower())
        return item
    metadata = finding.finding_metadata or {}
    raw_finding = metadata.get("raw_finding", {}) if isinstance(metadata, dict) else {}
    raw_poc = raw_finding.get("poc", {}) if isinstance(raw_finding, dict) else {}
    poc = {}
    if has_meaningful_poc(raw_poc) or finding.poc_description or finding.poc_steps:
        poc = {
            "description": finding.poc_description or raw_poc.get("description", ""),
            "steps": finding.poc_steps or raw_poc.get("steps", []),
            "payload": raw_poc.get("payload", ""),
            "impact": raw_poc.get("impact", ""),
            "cve_justification": raw_poc.get("cve_justification", ""),
        }
    exploit_chain = filter_meaningful_exploit_chain(raw_finding.get("exploit_chain", []))
    return {
        "id": finding.id,
        "task_id": finding.task_id,
        "title": finding.title,
        "severity": str(finding.severity),
        "finding_type": str(finding.vulnerability_type),
        "description": finding.description,
        "file_path": finding.file_path,
        "line_start": finding.line_start,
        "line_end": finding.line_end,
        "code_snippet": finding.code_snippet,
        "is_verified": finding.is_verified,
        "status": finding.status,
        "confidence": finding.ai_confidence,
        "ai_confidence": finding.ai_confidence,
        "suggestion": finding.suggestion,
        "has_poc": bool(getattr(finding, "has_poc", False) or has_meaningful_poc(raw_poc)),
        "poc_code": getattr(finding, "poc_code", None),
        "fix_code": getattr(finding, "fix_code", None),
        "ai_explanation": finding.ai_explanation,
        "origin": metadata.get("origin") or raw_finding.get("origin"),
        "evidence_type": metadata.get("evidence_type") or raw_finding.get("evidence_type"),
        "source": finding.source,
        "sink": finding.sink,
        "poc": poc,
        "exploit_chain": exploit_chain,
        "impact": raw_finding.get("impact", ""),
        "cve_justification": raw_finding.get("cve_justification", ""),
        "verification_notes": raw_finding.get("verification_notes", ""),
        "verdict": raw_finding.get("verdict", "candidate"),
        "report_status": raw_finding.get("report_status", raw_finding.get("verdict", "candidate")),
        "references": finding.references or raw_finding.get("references", []),
        "entry_point_refs": raw_finding.get("entry_point_refs", []),
        "priority_path_refs": raw_finding.get("priority_path_refs", []),
        "business_flow_notes": raw_finding.get("business_flow_notes", []),
        "evidence_gaps": raw_finding.get("evidence_gaps", []),
        "created_at": finding.created_at.isoformat() if finding.created_at else None,
    }


def get_default_report_template() -> Optional[Dict[str, Any]]:
    items = ReportTemplateFileService.list_templates()
    if not items:
        return None
    user_default = next((item for item in items if item.get("is_default")), None)
    if user_default:
        return user_default
    return items[0]


async def get_task_report(db: AsyncSession, task_id: str) -> Optional[AgentTaskReport]:
    result = await db.execute(select(AgentTaskReport).where(AgentTaskReport.task_id == task_id))
    return result.scalar_one_or_none()


async def build_report_payload(
    db: AsyncSession,
    task: AgentTask,
    project: Project,
    findings: List[AgentFinding | Dict[str, Any]],
    template: Optional[Dict[str, Any]] = None,
) -> ReportPayload:
    del db
    finding_items = [serialize_finding(item) for item in findings]
    severity_distribution = _severity_counts(finding_items)
    origin_distribution = _origin_counts(finding_items)
    report_status_distribution = _report_status_counts(finding_items)
    verified_count = sum(1 for item in finding_items if item.get("is_verified"))
    final_conclusions = [
        item for item in finding_items
        if item.get("report_status") in {"confirmed", "candidate"}
    ]

    def _finding_item(item: Dict[str, Any]) -> ReportFindingItem:
        # 过渡期: raw dict 可能仍是 vulnerability_type 键, 归一化到审计语义 finding_type。
        normalized = dict(item)
        normalized.setdefault("finding_type", str(normalized.get("vulnerability_type") or "other"))
        return ReportFindingItem(**normalized)

    pr_meta = (task.agent_config or {}).get("pr_meta") or {}
    duration_ms = None
    if task.completed_at and task.started_at:
        duration_ms = int((task.completed_at - task.started_at).total_seconds() * 1000)

    return ReportPayload(
        report={
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "type": "audit_report",
        },
        pr=ReportPRInfo(
            pr_url=pr_meta.get("pr_url"),
            pr_number=pr_meta.get("pr_number"),
            title=pr_meta.get("title"),
            branch=pr_meta.get("branch") or task.branch_name,
            base_sha=pr_meta.get("base_sha") or task.commit_sha,
            head_sha=pr_meta.get("head_sha"),
            author=pr_meta.get("author"),
        ),
        project={
            "id": project.id,
            "name": project.name,
            "source_type": getattr(project, "source_type", None),
        },
        task={
            "id": task.id,
            "status": task.status,
            "phase": task.current_phase,
            "name": task.name,
        },
        summary=ReportSummary(
            security_score=task.security_score,
            total_files_analyzed=task.analyzed_files,
            total_findings=len(finding_items),
            verified_findings=verified_count,
            confirmed_findings=report_status_distribution["confirmed"],
            candidate_findings=report_status_distribution["candidate"],
            false_positive_findings=report_status_distribution["false_positive"],
            severity_distribution=severity_distribution,
            origin_distribution=origin_distribution,
            total_iterations=task.total_iterations or 0,
            tool_calls_count=task.tool_calls_count or 0,
            tokens_used=task.tokens_used or 0,
            # 07-P2: 前缀缓存命中率(每视角最新一轮估算, 来自运行中 sink 持久化的 token_stats)
            cache_hit_ratio=((task.agent_config or {}).get("token_stats") or {}).get("cache_hit_ratio"),
            max_iterations=task.max_iterations,
            token_budget=task.token_budget,
            duration_ms=duration_ms,
            # 存量兼容键(模板执行摘要仍读 false_positive_count)
            false_positive_count=task.false_positive_count or 0,
            report_status_distribution=report_status_distribution,
        ),
        findings=[_finding_item(item) for item in finding_items],
        final_conclusions=[_finding_item(item) for item in final_conclusions],
        template={
            "id": template["slug"] if template else None,
            "name": template["name"] if template else None,
            "metadata_json": template.get("metadata_json", {}) if template else {},
        },
    )


def render_report_content(
    payload: ReportPayload | Dict[str, Any],
    template_content: str,
    output_format: str = "markdown",
) -> str:
    if isinstance(payload, ReportPayload):
        payload = payload.model_dump()
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)

    rendered = Template(template_content).render(**payload)
    if output_format == "html":
        escaped = rendered.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'><title>CodeSage PR 审计报告</title>"
            "<style>body{font-family:'Microsoft YaHei','PingFang SC',sans-serif;background:#f7f4ee;color:#24303f;padding:40px;}"
            ".page{max-width:1080px;margin:0 auto;background:white;border:1px solid #e6ddcf;border-radius:24px;"
            "box-shadow:0 30px 80px rgba(77,67,49,.12);padding:36px;}"
            "pre{white-space:pre-wrap;background:#fbf8f2;border:1px solid #eadfce;padding:18px;border-radius:18px;}"
            f"</style></head><body><div class='page'><pre>{escaped}</pre></div></body></html>"
        )
    return rendered


async def generate_task_report(
    db: AsyncSession,
    task: AgentTask,
    project: Project,
    findings: List[AgentFinding | Dict[str, Any]],
    template_id: Optional[str] = None,
    output_format: str = "markdown",
) -> AgentTaskReport:
    template = None
    if template_id:
        try:
            template = ReportTemplateFileService.read_template(template_id)
        except Exception:  # noqa: BLE001
            template = None
    if template is None:
        template = get_default_report_template()

    template_content = template["content"] if template else DEFAULT_REPORT_TEMPLATE
    final_format = output_format or (template["output_format"] if template else "markdown")
    payload = await build_report_payload(db, task, project, findings, template)
    payload_dict = payload.model_dump()
    content = render_report_content(payload_dict, template_content, final_format)

    report = await get_task_report(db, task.id)
    if report is None:
        report = AgentTaskReport(task_id=task.id)
        db.add(report)

    report.template_id = template["slug"] if template else None
    report.output_format = final_format
    report.title = f"CodeSage-{project.name}-audit-report"
    report.content = content
    report.report_json = payload_dict
    report.report_metadata = {
        "generated_at": payload_dict["report"]["generated_at"],
        "report_type": payload_dict["report"]["type"],
        "template_name": template["name"] if template else "系统默认模板",
        "template_path": template.get("template_file") if template else None,
        "template_slug": template["slug"] if template else None,
    }
    await db.flush()
    return report
