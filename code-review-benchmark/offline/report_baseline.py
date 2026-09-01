# -*- coding: utf-8 -*-
"""汇总 codesage 基线: evaluations.json → 整体指标 + 分视角 + 对比。

用法(offline/ 下):
    python report_baseline.py --model-dir results/DeepSeek-V4-Flash-0731 --tools codesage codesage-runtime
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

OFFLINE = Path(__file__).resolve().parent
BACKEND = Path(r"E:\Mac\CodeSage\backend")
sys.path.insert(0, str(BACKEND))

from app.services.pr_review.eval_gate import (  # noqa: E402
    check_gate,
    compute_metrics,
    perspective_breakdown,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--tools", nargs="+", required=True)
    args = parser.parse_args()

    evals_path = Path(args.model_dir) / "evaluations.json"
    all_evals = json.loads(evals_path.read_text(encoding="utf-8"))
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "judge_model": Path(args.model_dir).name, "tools": {}}

    per_tool = {}
    for tool in args.tools:
        subset = {url: e for url, e in all_evals.items()
                  if isinstance(e, dict) and tool in (e.get("tools") or [e.get("tool")])}
        if not subset:
            # evaluations.json 结构2: {pr_url: {tool: entry}} 或 {pr_url: entry}
            subset = {url: (e[tool] if isinstance(e, dict) and tool in e else e)
                      for url, e in all_evals.items() if isinstance(e, dict) and (tool in e or e.get("tool") == tool)}
        if not subset:
            report["tools"][tool] = {"error": "no evaluations found"}
            continue
        m = compute_metrics(subset)
        pb = perspective_breakdown(subset)
        per_tool[tool] = subset
        report["tools"][tool] = {
            "prs": len(subset),
            "overall": m.as_dict(),
            "perspectives": pb,
        }

    if "codesage" in per_tool and "codesage-runtime" in per_tool:
        gate = check_gate(per_tool["codesage"], per_tool["codesage-runtime"])
        report["gate_rules_vs_runtime"] = gate.as_dict()

    out = Path(args.model_dir) / "codesage_baseline_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
