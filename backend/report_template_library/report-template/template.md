# AutoCVE 最终漏洞报告

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
- 已验证漏洞: {{ summary.verified_findings }}
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
- 总耗时(ms): {{ summary.duration_ms if summary.duration_ms is not none else 'N/A' }}

## 漏洞清单
{% if findings %}
{% for finding in findings %}
### {{ loop.index }}. [{{ finding.severity|upper }}] {{ finding.title }}
- 漏洞类型: {{ finding.vulnerability_type }}
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
本次任务未生成可输出的漏洞结果。
{% endif %}

## 修复优先级建议
1. 优先修复 Critical / High 严重等级问题。
2. 结合 Scan/Triage 与 Finding 两条线索统一排期。
3. 对已验证漏洞优先补充修复与回归验证。
