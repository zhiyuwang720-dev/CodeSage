"""Step 3.5 — 两次评测的回归对比快照(阶段 03 §3.1)。

输入: step3 产出的两份 evaluations.json(results/{judge_model}/evaluations.json)。
输出: 整体 delta + 逐 PR delta + 高危退化清单 + 分视角归因(JSON 或 Markdown)。

用法:
    python -m code_review_benchmark.step3_5_snapshot \
        --baseline results/judgeA/evaluations.json \
        --current  results/judgeA/evaluations_v2.json \
        --output snapshot_report.json --format json

门禁判定在 CodeSage 侧 eval_gate.check_gate(同一 delta 语义), 本脚本负责呈现。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HIGH_SEVERITIES = frozenset({"high", "critical"})
SOURCE_PREFIX_RE = re.compile(r"^\s*\[([A-Za-z][A-Za-z_-]*)\]")


def load_evaluations(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _counts(entry: dict) -> tuple[int, int, int]:
    return int(entry.get("tp") or 0), int(entry.get("fp") or 0), int(entry.get("fn") or 0)


def _golden_key(comment: str) -> str:
    return re.sub(r"\s+", " ", str(comment or "")).strip().lower()


def _high_severity_goldens(evaluations: dict, *, only_tp: bool) -> set[str]:
    keys: set[str] = set()
    for entry in evaluations.values():
        if not isinstance(entry, dict) or entry.get("skipped"):
            continue
        if only_tp:
            items = entry.get("true_positives") or []
        else:
            items = entry.get("false_negatives") or []
        for item in items:
            if str((item or {}).get("severity") or "").lower() in HIGH_SEVERITIES:
                keys.add(_golden_key(item.get("golden_comment")))
    keys.discard("")
    return keys


def diff_evaluations(baseline: dict, current: dict) -> dict:
    """与 CodeSage eval_gate.snapshot_diff 同语义(独立实现, 不引入 backend 依赖)。"""
    base_tp = sum(_counts(e)[0] for e in baseline.values() if isinstance(e, dict) and not e.get("skipped"))
    base_fp = sum(_counts(e)[1] for e in baseline.values() if isinstance(e, dict) and not e.get("skipped"))
    base_fn = sum(_counts(e)[2] for e in baseline.values() if isinstance(e, dict) and not e.get("skipped"))
    curr_tp = sum(_counts(e)[0] for e in current.values() if isinstance(e, dict) and not e.get("skipped"))
    curr_fp = sum(_counts(e)[1] for e in current.values() if isinstance(e, dict) and not e.get("skipped"))
    curr_fn = sum(_counts(e)[2] for e in current.values() if isinstance(e, dict) and not e.get("skipped"))
    base_p = base_tp / (base_tp + base_fp) if (base_tp + base_fp) else 0.0
    base_r = base_tp / (base_tp + base_fn) if (base_tp + base_fn) else 0.0
    curr_p = curr_tp / (curr_tp + curr_fp) if (curr_tp + curr_fp) else 0.0
    curr_r = curr_tp / (curr_tp + curr_fn) if (curr_tp + curr_fn) else 0.0

    prs = {}
    for pr_url in sorted(set(baseline) | set(current)):
        b = baseline.get(pr_url) or {}
        c = current.get(pr_url) or {}
        if (isinstance(b, dict) and b.get("skipped")) and (isinstance(c, dict) and c.get("skipped")):
            continue
        delta = {
            "tp": _counts(c)[0] - _counts(b)[0],
            "fp": _counts(c)[1] - _counts(b)[1],
            "fn": _counts(c)[2] - _counts(b)[2],
        }
        prs[pr_url] = {"delta": delta, "changed": any(delta.values())}

    return {
        "overall": {
            "baseline": {
                "tp": base_tp, "fp": base_fp, "fn": base_fn,
                "precision": round(base_p, 4), "recall": round(base_r, 4),
            },
            "current": {
                "tp": curr_tp, "fp": curr_fp, "fn": curr_fn,
                "precision": round(curr_p, 4), "recall": round(curr_r, 4),
            },
            "delta": {
                "precision": round(curr_p - base_p, 4), "recall": round(curr_r - base_r, 4),
                "tp": curr_tp - base_tp, "fp": curr_fp - base_fp, "fn": curr_fn - base_fn,
            },
        },
        "prs": prs,
        "changed_prs": sum(1 for p in prs.values() if p["changed"]),
        "high_severity_regressions": sorted(_high_severity_goldens(baseline, only_tp=True) & _high_severity_goldens(current, only_tp=False)),
    }


def perspective_breakdown(evaluations: dict) -> dict[str, dict]:
    """分视角归因: TP/FP 按候选文本 [Label] 前缀; FN 不可归因只计数。"""
    buckets: dict[str, dict[str, int]] = {}

    def _bucket(name: str) -> dict[str, int]:
        return buckets.setdefault(name, {"tp": 0, "fp": 0, "fn": 0})

    for entry in evaluations.values():
        if not isinstance(entry, dict) or entry.get("skipped"):
            continue
        for item in entry.get("true_positives") or []:
            match = SOURCE_PREFIX_RE.match(str((item or {}).get("matched_candidate") or ""))
            _bucket(match.group(1).lower() if match else "unattributed")["tp"] += 1
        for item in entry.get("false_positives") or []:
            match = SOURCE_PREFIX_RE.match(str((item or {}).get("candidate") or ""))
            _bucket(match.group(1).lower() if match else "unattributed")["fp"] += 1
        for _ in entry.get("false_negatives") or []:
            _bucket("unattributed")["fn"] += 1

    return {
        name: {
            **counts,
            "precision": round(counts["tp"] / (counts["tp"] + counts["fp"]), 4)
            if (counts["tp"] + counts["fp"]) else 0.0,
        }
        for name, counts in sorted(buckets.items())
    }


def to_markdown(report: dict) -> str:
    overall = report["overall"]
    lines = [
        "# 评测回归快照",
        "",
        f"- 基线: precision {overall['baseline']['precision']:.3f} / recall {overall['baseline']['recall']:.3f}"
        f" (TP {overall['baseline']['tp']} / FP {overall['baseline']['fp']} / FN {overall['baseline']['fn']})",
        f"- 当前: precision {overall['current']['precision']:.3f} / recall {overall['current']['recall']:.3f}"
        f" (TP {overall['current']['tp']} / FP {overall['current']['fp']} / FN {overall['current']['fn']})",
        f"- delta: precision {overall['delta']['precision']:+.3f} / recall {overall['delta']['recall']:+.3f}",
        f"- 变化 PR: {report['changed_prs']}",
        "",
    ]
    if report["high_severity_regressions"]:
        lines += ["## ⚠ 高危回归(TP→FN)", ""]
        lines += [f"- {g[:100]}" for g in report["high_severity_regressions"]]
        lines.append("")
    lines += ["## 逐 PR delta", "", "| PR | ΔTP | ΔFP | ΔFN |", "|---|---|---|---|"]
    lines += [
        f"| {url.rsplit('/', 1)[-1]} | {p['delta']['tp']:+d} | {p['delta']['fp']:+d} | {p['delta']['fn']:+d} |"
        for url, p in report["prs"].items() if p["changed"]
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测回归快照(step 3.5)")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--output", help="报告输出路径(缺省打印 stdout)")
    parser.add_argument("--format", default="json", choices=["json", "md"])
    args = parser.parse_args(argv)

    report = diff_evaluations(load_evaluations(Path(args.baseline)), load_evaluations(Path(args.current)))
    if args.format == "md":
        rendered = to_markdown(report)
    else:
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"报告已写入 {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
