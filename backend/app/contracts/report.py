"""08-P2: 报告导出语义模型(audit_report)。

build_report_payload 返回 ReportPayload,承载:
- PR 审计基本信息(pr 块, 来源 task.agent_config["pr_meta"])
- 审计统计(max_iterations/token_budget/duration_ms + P1 实时累计值)
- 审计发现清单(finding_type 替代旧漏洞语义 vulnerability_type)

过渡期兼容: ReportFindingItem 保留 extra="allow"(Plan C 数据模型治理收严),
期间 report 相关字段名已按审计语义命名。
"""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class ReportPRInfo(BaseModel):
    """PR 审计基本信息(模板开头渲染; 缺失字段按 None 兜底)。"""

    pr_url: str | None = None
    pr_number: int | None = None
    title: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    author: str | None = None


class ReportFindingItem(BaseModel):
    """审计发现条目: finding_type 替代旧 vulnerability_type(审计语义)。

    description/file_path 可缺省(原始 finding 常无描述; 模板按 or 'N/A'/'无' 兜底)。
    """

    finding_type: str
    severity: str
    title: str
    description: str | None = None
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    model_config = ConfigDict(extra="allow")  # 过渡期兼容, Plan C 收严


class ReportSummary(BaseModel):
    """运行统计 + 发现分布(P1 实时累计值 + AgentTask 参数)。"""

    security_score: float | None = None
    total_files_analyzed: int | None = None
    total_findings: int = 0
    verified_findings: int = 0
    confirmed_findings: int = 0
    candidate_findings: int = 0
    false_positive_findings: int = 0
    severity_distribution: Dict[str, Any] = Field(default_factory=dict)
    origin_distribution: Dict[str, Any] = Field(default_factory=dict)
    total_iterations: int = 0
    tool_calls_count: int = 0
    tokens_used: int = 0
    cache_hit_ratio: float | None = None  # 07-P2: 前缀缓存命中率(来自 agent_config["token_stats"])
    max_iterations: int | None = None
    token_budget: int | None = None
    duration_ms: int | None = None

    model_config = ConfigDict(extra="allow")  # 承载存量 false_positive_count 等


class ReportPayload(BaseModel):
    """报告渲染 payload(替代裸 dict): 模板 + JSON 导出共用一份定型结构。"""

    report: Dict[str, Any]
    pr: ReportPRInfo
    project: Dict[str, Any]
    task: Dict[str, Any]
    summary: ReportSummary
    findings: List[ReportFindingItem]
    final_conclusions: List[ReportFindingItem]
    template: Dict[str, Any]
