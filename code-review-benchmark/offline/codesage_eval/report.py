from __future__ import annotations

import html
import json
from pathlib import Path

from codesage_eval.contracts import DatasetCase, EvalCaseResult, JudgmentRecord
from codesage_eval.scoring import aggregate_micro, score_case
from codesage_eval.storage import write_jsonl_atomic


def build_summary(
    cases: list[DatasetCase],
    results: list[EvalCaseResult],
    judgments: list[JudgmentRecord],
) -> dict:
    case_by_id = {item.case_id: item for item in cases}
    judgments_by_case: dict[str, list[JudgmentRecord]] = {}
    for item in judgments:
        judgments_by_case.setdefault(item.case_id, []).append(item)
    rows = []
    result_by_id = {item.case_id: item for item in results}
    for case_id in sorted(case_by_id):
        case = case_by_id[case_id]
        result = result_by_id.get(case_id) or EvalCaseResult(
            eval_run_id="missing", case_id=case_id, status="failed", error_kind="result_missing"
        )
        case_judgments = judgments_by_case.get(result.case_id, [])
        unknown = sum(item.matched is None for item in case_judgments)
        expected = len(case.golden) * len(result.candidates)
        quality_complete = result.execution_complete and unknown == 0 and len(case_judgments) == expected
        score = score_case(case.golden, result.candidates, case_judgments)
        if not result.execution_complete:
            score = score_case(case.golden, [], [])
        rows.append(
            {
                "case_id": result.case_id,
                "repo": case.repo,
                "status": result.status,
                "execution_complete": result.execution_complete,
                "quality_complete": quality_complete,
                "trace_complete": result.trace_complete,
                "usage_complete": result.usage_complete,
                "unknown_judgments": unknown,
                "expected_judgments": expected,
                "observed_judgments": len(case_judgments),
                **score,
            }
        )
    completed_scores = [row for row in rows if row["quality_complete"]]
    all_delivery_scores = [
        row if row["execution_complete"] else {"tp": 0, "fp": 0, "fn": row["fn"]}
        for row in rows
    ]
    return {
        "schema_version": "1",
        "case_count": len(rows),
        "execution_completed": sum(row["execution_complete"] for row in rows),
        "quality_completed": len(completed_scores),
        "trace_completed": sum(row["trace_complete"] for row in rows),
        "usage_completed": sum(row["usage_complete"] for row in rows),
        "conditional_quality": aggregate_micro(completed_scores),
        "effective_delivery": aggregate_micro(all_delivery_scores),
        "cases": rows,
    }


def write_report(run_dir: str | Path, summary: dict, *, public: bool = False) -> Path:
    destination = Path(run_dir)
    destination.mkdir(parents=True, exist_ok=True)
    write_jsonl_atomic(destination / "summary.jsonl", [summary])
    safe_json = json.dumps(summary, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item['case_id']))}</td>"
        f"<td>{html.escape(str(item['repo']))}</td>"
        f"<td>{html.escape(str(item['status']))}</td>"
        f"<td>{item['tp']}/{item['fp']}/{item['fn']}</td>"
        f"<td>{item['f1']:.3f}</td>"
        "</tr>"
        for item in summary["cases"]
    )
    title = "CodeSage Public Benchmark Report" if public else "CodeSage Benchmark Report"
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>
body{{font:14px system-ui;margin:2rem;color:#172033;background:#f7f8fb}}main{{max-width:1100px;margin:auto}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem}}.card,table{{background:white;border:1px solid #dfe3eb;border-radius:8px}}
.card{{padding:1rem}}table{{width:100%;border-collapse:collapse;margin-top:1.5rem}}th,td{{padding:.65rem;border-bottom:1px solid #eee;text-align:left}}
</style></head><body><main><h1>{title}</h1><div class="cards">
<div class="card">Cases<br><strong>{summary['case_count']}</strong></div>
<div class="card">Execution complete<br><strong>{summary['execution_completed']}</strong></div>
<div class="card">Quality complete<br><strong>{summary['quality_completed']}</strong></div>
<div class="card">Micro F1<br><strong>{summary['conditional_quality']['f1']:.3f}</strong></div>
</div><table><thead><tr><th>Case</th><th>Repo</th><th>Status</th><th>TP/FP/FN</th><th>F1</th></tr></thead><tbody>{rows}</tbody></table>
<script type="application/json" id="codesage-summary">{safe_json}</script></main></body></html>"""
    output = destination / "report.html"
    output.write_text(document, encoding="utf-8", newline="\n")
    return output
