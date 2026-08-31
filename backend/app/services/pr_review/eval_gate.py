"""阶段 03 — 评测门禁与回归对比(spec 03 §3.1 eval_gate.py)。

纯逻辑、离线、可确定性测试:
- compute_metrics: 汇总 step3 evaluations.json → precision/recall/TP/FP/FN + 高危 FN 清单
- check_gate: 门禁规则(recall 下降>5% / 新增 FP>10% / 高危 golden TP→FN 任一 → 红)
- snapshot_diff: 两次评测的逐 PR delta + 整体 delta(回归报告数据源)
- perspective_breakdown: 按 "[Security]" 类前缀做分视角归因(spec §7 约定)

输入契约(与 benchmark step3_judge_comments.py 输出对齐):
evaluations: dict[pr_url, {"skipped": bool, "tp": int, "fp": int, "fn": int,
    "precision": float, "recall": float,
    "true_positives": [{"golden_comment", "severity", "category", "matched_candidate"...}],
    "false_positives": [{"candidate": str}], "false_negatives": [{"golden_comment", "severity", "category"}]}]
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# 高危 golden(阻塞级)
HIGH_SEVERITIES = frozenset({"high", "critical"})
# spec §7.105: 评论 body 前缀 [Security]/[Architecture]/[Quality]/[Rules] → source
SOURCE_PREFIX_RE = re.compile(r"^\s*\[([A-Za-z][A-Za-z_-]*)\]")


def source_of_comment(body: str) -> str | None:
    """从评论 body 提取 [Label] 前缀 → 小写 source; 无前缀 → None(计入未归属)。"""
    match = SOURCE_PREFIX_RE.match(body or "")
    return match.group(1).lower() if match else None


@dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    high_severity_fn: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn,
            "precision": round(self.precision, 4), "recall": round(self.recall, 4),
            "high_severity_fn": list(self.high_severity_fn),
        }


def _is_skipped(entry: Mapping) -> bool:
    return bool(entry.get("skipped"))


def compute_metrics(evaluations: Mapping[str, Mapping]) -> Metrics:
    """聚合全部 PR 的评测条目(跳过 skipped 条目)。"""
    metrics = Metrics()
    for pr_url, entry in evaluations.items():
        if not isinstance(entry, Mapping) or _is_skipped(entry):
            continue
        metrics.tp += int(entry.get("tp") or 0)
        metrics.fp += int(entry.get("fp") or 0)
        metrics.fn += int(entry.get("fn") or 0)
        for item in entry.get("false_negatives") or []:
            severity = str((item or {}).get("severity") or "").lower()
            if severity in HIGH_SEVERITIES:
                golden = str((item or {}).get("golden_comment") or "")
                if golden:
                    metrics.high_severity_fn.append(f"{pr_url}: {golden[:80]}")
    metrics.precision = metrics.tp / (metrics.tp + metrics.fp) if (metrics.tp + metrics.fp) else 0.0
    metrics.recall = metrics.tp / (metrics.tp + metrics.fn) if (metrics.tp + metrics.fn) else 0.0
    return metrics


@dataclass
class GateResult:
    passed: bool
    reasons: list[str]
    baseline: Metrics
    current: Metrics

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "baseline": self.baseline.as_dict(),
            "current": self.current.as_dict(),
        }


def check_gate(
    baseline: Mapping[str, Mapping],
    current: Mapping[str, Mapping],
    *,
    recall_drop_threshold: float = 0.05,
    fp_growth_threshold: float = 0.10,
    high_severity_regression_blocks: bool = True,
) -> GateResult:
    """门禁(spec 03 §3.3): recall 降>阈值 / 新增 FP 超基线 10% / 高危 TP→FN → 红。"""
    base = compute_metrics(baseline)
    curr = compute_metrics(current)
    reasons: list[str] = []

    recall_drop = base.recall - curr.recall
    if recall_drop > recall_drop_threshold:
        reasons.append(
            f"recall 下降 {recall_drop:.3f}(基线 {base.recall:.3f} → 当前 {curr.recall:.3f},"
            f" 阈值 {recall_drop_threshold})"
        )

    if base.fp == 0:
        fp_growth = 1.0 if curr.fp > 0 else 0.0
    else:
        fp_growth = (curr.fp - base.fp) / base.fp
    if fp_growth > fp_growth_threshold:
        reasons.append(
            f"新增 FP {fp_growth:.1%}(基线 {base.fp} → 当前 {curr.fp}, 阈值 {fp_growth_threshold:.0%})"
        )

    if high_severity_regression_blocks:
        baseline_tp_goldens = _high_severity_goldens(baseline, only_tp=True)
        current_fn_goldens = _high_severity_goldens(current, only_tp=False)
        regressed = baseline_tp_goldens & current_fn_goldens
        if regressed:
            reasons.append(f"高危 golden 从 TP 退化为 FN: {len(regressed)} 条")

    return GateResult(passed=not reasons, reasons=reasons, baseline=base, current=curr)


def _golden_key(comment: str) -> str:
    return re.sub(r"\s+", " ", str(comment or "")).strip().lower()


def _high_severity_goldens(evaluations: Mapping[str, Mapping], *, only_tp: bool) -> set[str]:
    keys: set[str] = set()
    for entry in evaluations.values():
        if not isinstance(entry, Mapping) or _is_skipped(entry):
            continue
        if only_tp:
            for item in entry.get("true_positives") or []:
                severity = str((item or {}).get("severity") or "").lower()
                if severity in HIGH_SEVERITIES:
                    keys.add(_golden_key((item or {}).get("golden_comment")))
        else:
            for item in entry.get("false_negatives") or []:
                severity = str((item or {}).get("severity") or "").lower()
                if severity in HIGH_SEVERITIES:
                    keys.add(_golden_key((item or {}).get("golden_comment")))
    keys.discard("")
    return keys


def snapshot_diff(
    baseline: Mapping[str, Mapping],
    current: Mapping[str, Mapping],
) -> dict:
    """两次评测的回归对比: 整体 delta + 逐 PR delta + 高危退化清单。"""
    base_metrics = compute_metrics(baseline)
    curr_metrics = compute_metrics(current)
    base_tp = _high_severity_goldens(baseline, only_tp=True)
    curr_fn = _high_severity_goldens(current, only_tp=False)

    pr_reports: dict[str, dict] = {}
    for pr_url in sorted(set(baseline) | set(current)):
        b = baseline.get(pr_url) or {}
        c = current.get(pr_url) or {}
        if _is_skipped(b) and _is_skipped(c):
            continue
        delta = {
            "tp": int(c.get("tp") or 0) - int(b.get("tp") or 0),
            "fp": int(c.get("fp") or 0) - int(b.get("fp") or 0),
            "fn": int(c.get("fn") or 0) - int(b.get("fn") or 0),
        }
        pr_reports[pr_url] = {
            "delta": delta,
            "changed": any(delta.values()),
            "baseline_skipped": _is_skipped(b),
            "current_skipped": _is_skipped(c),
        }

    regressed = sorted(base_tp & curr_fn)
    return {
        "overall": {
            "baseline": base_metrics.as_dict(),
            "current": curr_metrics.as_dict(),
            "delta": {
                "precision": round(curr_metrics.precision - base_metrics.precision, 4),
                "recall": round(curr_metrics.recall - base_metrics.recall, 4),
                "tp": curr_metrics.tp - base_metrics.tp,
                "fp": curr_metrics.fp - base_metrics.fp,
                "fn": curr_metrics.fn - base_metrics.fn,
            },
        },
        "prs": pr_reports,
        "changed_prs": sum(1 for p in pr_reports.values() if p["changed"]),
        "high_severity_regressions": regressed,
    }


def perspective_breakdown(evaluations: Mapping[str, Mapping]) -> dict[str, dict]:
    """分视角归因(spec 03 §3.2): TP/FP 按 [Label] 前缀归属; 未归属计入 unattributed。

    FN 无法归因(judge 端 golden 无来源), 只统计数量。
    """
    buckets: dict[str, dict[str, int]] = {}

    def _bucket(name: str) -> dict[str, int]:
        return buckets.setdefault(name, {"tp": 0, "fp": 0, "fn": 0})

    for entry in evaluations.values():
        if not isinstance(entry, Mapping) or _is_skipped(entry):
            continue
        for item in entry.get("true_positives") or []:
            source = source_of_comment(str((item or {}).get("matched_candidate") or "")) or "unattributed"
            _bucket(source)["tp"] += 1
        for item in entry.get("false_positives") or []:
            source = source_of_comment(str((item or {}).get("candidate") or "")) or "unattributed"
            _bucket(source)["fp"] += 1
        for _ in entry.get("false_negatives") or []:
            _bucket("unattributed")["fn"] += 1

    breakdown: dict[str, dict] = {}
    for name, counts in sorted(buckets.items()):
        tp, fp = counts["tp"], counts["fp"]
        breakdown[name] = {
            **counts,
            "precision": round(tp / (tp + fp), 4) if (tp + fp) else 0.0,
        }
    return breakdown
