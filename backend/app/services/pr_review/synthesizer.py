"""综合层(阶段 02 §3.3): 三视角原始结果 → PR 评论 的确定性管道。

放在 Orchestrator 内调用(复用 AutoCVE _merge_findings 框架思路):
归一化 → 去重(file+line+category) → 严重度合并(取最高) → 排序限条数 → 落行校验。
纯函数无 LLM, 可复现可测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from app.services.pr_review.diff_lines import added_line_index
from app.services.review_runtime.final_review_contract import (
    SEVERITY_RANK,
    ReviewFinding,
)

# 视角 source → 评论前缀标签(spec 03 §7 分视角归约约定)
SOURCE_LABELS = {
    "security": "Security",
    "architecture": "Architecture",
    "quality": "Quality",
    "rules": "Rules",
    "orchestrator": "Orchestrator",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(str(source or "").lower(), str(source or "Unattributed").capitalize())


@dataclass
class SynthesisResult:
    comments: list[ReviewFinding] = field(default_factory=list)
    deduped_away: int = 0
    rejected_off_diff: int = 0
    needs_verification: bool = False


def normalize_finding(raw: dict) -> ReviewFinding | None:
    """单条原始结果 → ReviewFinding(补默认值/校验路径); 不可恢复的丢弃。"""
    if isinstance(raw, ReviewFinding):
        return raw
    if not isinstance(raw, dict):
        return None
    data = dict(raw)
    data.setdefault("needs_verification", False)
    data.setdefault("verdict", "suspected")
    data.setdefault("confidence", 0.5)
    data.setdefault("source", "unknown")
    try:
        return ReviewFinding.model_validate(data)
    except ValidationError:
        return None


def merge_dedup(findings: list[ReviewFinding]) -> tuple[list[ReviewFinding], int]:
    """按 (file_path, line_start, category) 去重: 同 key 保留严重度最高(再按置信度),
    合并描述(附加第二来源标记), 来源标注保留。"""
    best: dict[tuple[str, int, str], ReviewFinding] = {}
    order: list[tuple[str, int, str]] = []
    for item in findings:
        key = item.dedup_key()
        if key not in best:
            best[key] = item
            order.append(key)
            continue
        kept = best[key]
        if SEVERITY_RANK[item.severity] > SEVERITY_RANK[kept.severity] or (
            SEVERITY_RANK[item.severity] == SEVERITY_RANK[kept.severity]
            and item.confidence > kept.confidence
        ):
            merged_source = f"{kept.source}+{item.source}" if item.source not in kept.source else kept.source
            best[key] = item.model_copy(update={"source": merged_source})
        else:
            kept_source = kept.source
            if item.source not in kept_source:
                best[key] = kept.model_copy(update={"source": f"{kept_source}+{item.source}"})
    return [best[key] for key in order], len(findings) - len(best)


def rank_and_limit(
    findings: list[ReviewFinding],
    *,
    max_comments: int = 10,
    min_severity: str = "high",
) -> list[ReviewFinding]:
    """严重度+置信度排序; 低噪原则: 初始只保留 >= min_severity(§3.3.4)。"""
    floor = SEVERITY_RANK.get(min_severity, 3)
    eligible = [f for f in findings if SEVERITY_RANK[f.severity] >= floor]
    eligible.sort(key=lambda f: (-SEVERITY_RANK[f.severity], -f.confidence, f.file_path, f.line_start))
    return eligible[:max_comments]


def enforce_added_lines(
    findings: list[ReviewFinding], diff_text: str
) -> tuple[list[ReviewFinding], int]:
    """评论 line 必须落在 diff 新增行(head 行号); 违规丢弃计数(spec §2 原则③)。"""
    index = added_line_index(diff_text)
    valid: list[ReviewFinding] = []
    rejected = 0
    for item in findings:
        lines = index.get(item.file_path)
        if lines is None:
            rejected += 1
            continue
        if item.line_start in lines or (item.line_start <= item.line_end and any(l in lines for l in range(item.line_start, min(item.line_end, item.line_start + 50) + 1))):
            valid.append(item)
        else:
            rejected += 1
    return valid, rejected


def synthesize(
    handoff_findings: list[dict],
    *,
    diff_text: str | None = None,
    max_comments: int = 10,
    min_severity: str = "high",
    enforce_lines: bool = True,
) -> SynthesisResult:
    """综合层主入口: handoff.key_findings 列表(原始 dict) → 最终评论集。"""
    result = SynthesisResult()
    normalized: list[ReviewFinding] = []
    for raw in handoff_findings:
        item = normalize_finding(raw)
        if item is None:
            continue
        normalized.append(item)

    merged, deduped = merge_dedup(normalized)
    result.deduped_away = deduped

    if enforce_lines and diff_text:
        merged, rejected = enforce_added_lines(merged, diff_text)
        result.rejected_off_diff = rejected

    result.comments = rank_and_limit(
        merged, max_comments=max_comments, min_severity=min_severity
    )
    result.needs_verification = any(f.needs_verification for f in result.comments)
    return result


def finding_to_comment(finding: ReviewFinding) -> dict:
    """ReviewFinding → benchmark 注入格式 {path, line, body, severity, category, source}。

    spec 03 §7.105: body 以 "[Security]/[Architecture]/[Quality]/[Rules]" 前缀开头,
    供评测管线做分视角归因(step3_5_snapshot / eval_gate.perspective_breakdown)。
    """
    body_lines = [f"[{source_label(finding.source)}] **{finding.title}**", "", finding.description]
    if finding.suggestion:
        body_lines += ["", f"建议: {finding.suggestion}"]
    if finding.code_snippet:
        body_lines += ["", f"```{'' if '.' not in finding.file_path else finding.file_path.rsplit('.', 1)[-1]}\n{finding.code_snippet}\n```"]
    body_lines += ["", f"({finding.rule_id} · 置信度 {finding.confidence:.2f} · 来源 {finding.source})"]
    return {
        "path": finding.file_path,
        "line": finding.line_start,
        "body": "\n".join(body_lines),
        "severity": finding.severity,
        "category": finding.category,
    }
