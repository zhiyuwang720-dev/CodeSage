#!/usr/bin/env python3
"""Export tool reviews to Excel.

Reads candidates and evaluations from results/{model}/ directory.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path

if importlib.util.find_spec("openpyxl") is None:
    print("Installing openpyxl...")
    import subprocess
    subprocess.run(["uv", "pip", "install", "openpyxl"], check=True)

from openpyxl import Workbook

RESULTS_DIR = Path("results")


def load_dotenv():
    """Load .env file into environment."""
    env_path = Path(".env")
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def sanitize_model_name(model: str) -> str:
    """Sanitize model name for use as directory name."""
    return model.strip().replace("/", "_")


def get_model_dir() -> Path:
    """Get the model-specific results directory, creating it if needed."""
    model = os.environ.get("MARTIAN_MODEL", "openai/gpt-4o-mini")
    model_dir = RESULTS_DIR / sanitize_model_name(model)
    model_dir.mkdir(parents=True, exist_ok=True)
    return model_dir


def export_tool(tool_name: str, data: dict, all_candidates: dict, evaluations: dict, model_dir: Path):
    """Export a single tool's reviews to Excel."""
    wb = Workbook()
    ws = wb.active
    ws.title = f"{tool_name} Reviews"

    headers = [
        "pr_id",
        "review_url",
        "review_text",
        "candidates",
        "last_comment",
        "golden_comments",
        "judge_results",
        "found_issues",
        "total_issues",
    ]
    ws.append(headers)

    for golden_url, entry in data.items():
        tool_review = None
        for review in entry.get("reviews", []):
            if review["tool"] == tool_name:
                tool_review = review
                break

        if not tool_review:
            continue

        pr_id = entry.get("original_url") or golden_url
        review_url = tool_review.get("pr_url", "")
        comments = tool_review.get("review_comments", [])
        review_text = "\n\n---\n\n".join(c.get("body", "") for c in comments if c.get("body"))

        candidates_for_tool = []
        if golden_url in all_candidates and tool_name in all_candidates[golden_url]:
            candidates_for_tool = all_candidates[golden_url][tool_name]
        candidates = "\n\n".join(c.get("text", "") for c in candidates_for_tool)

        last_comment = comments[-1].get("body", "") if comments else ""

        golden_comments = "\n\n".join(
            f"[{gc.get('severity', 'Unknown')}] {gc.get('comment', '')}"
            for gc in entry.get("golden_comments", [])
        )

        eval_result = evaluations.get(golden_url, {}).get(tool_name, {})
        found_issues = eval_result.get("tp", "")
        total_issues = eval_result.get("total_golden", "")

        if eval_result and not eval_result.get("skipped"):
            tp_list = eval_result.get("true_positives", [])
            false_negs = eval_result.get("false_negatives", [])
            judge_lines = []
            for m in tp_list:
                judge_lines.append(f"[FOUND] [{m.get('severity', '?')}] {m.get('golden_comment', '')}")
            for fn in false_negs:
                judge_lines.append(f"[MISSED] [{fn.get('severity', '?')}] {fn.get('golden_comment', '')}")
            judge_results = "\n".join(judge_lines)
        else:
            judge_results = eval_result.get("reason", "") if eval_result else "Not evaluated"

        ws.append([
            pr_id,
            review_url,
            review_text,
            candidates,
            last_comment,
            golden_comments,
            judge_results,
            found_issues,
            total_issues,
        ])

    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 80
    ws.column_dimensions["C"].width = 100
    ws.column_dimensions["D"].width = 50
    ws.column_dimensions["E"].width = 100
    ws.column_dimensions["F"].width = 100
    ws.column_dimensions["G"].width = 80
    ws.column_dimensions["H"].width = 15
    ws.column_dimensions["I"].width = 15

    export_dir = model_dir / "tool_exports"
    export_dir.mkdir(exist_ok=True)
    output_path = export_dir / f"{tool_name}_reviews.xlsx"
    wb.save(output_path)
    print(f"  {output_path} ({ws.max_row - 1} rows)")


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Export tool reviews to Excel")
    parser.add_argument("--tool", help="Tool name to export (default: all tools)")
    args = parser.parse_args()

    data_path = RESULTS_DIR / "benchmark_data.json"
    model_dir = get_model_dir()
    candidates_path = model_dir / "candidates.json"
    eval_path = model_dir / "evaluations.json"

    print(f"Model dir: {model_dir}")

    if not data_path.exists():
        print(f"Error: {data_path} not found")
        return

    with open(data_path) as f:
        data = json.load(f)

    # Load model-specific candidates
    all_candidates = {}
    if candidates_path.exists():
        with open(candidates_path) as f:
            all_candidates = json.load(f)
        print(f"Loaded candidates from {candidates_path}")

    # Load model-specific evaluations
    evaluations = {}
    if eval_path.exists():
        with open(eval_path) as f:
            evaluations = json.load(f)
        print(f"Loaded evaluations for {len(evaluations)} PRs")

    # Discover all tools from data
    all_tools = set()
    for entry in data.values():
        for review in entry.get("reviews", []):
            all_tools.add(review["tool"])

    # Filter to specified tool or export all
    if args.tool:
        if args.tool not in all_tools:
            print(f"Error: tool '{args.tool}' not found. Available: {sorted(all_tools)}")
            return
        tools_to_export = [args.tool]
    else:
        tools_to_export = sorted(all_tools)

    print(f"Exporting {len(tools_to_export)} tool(s): {', '.join(tools_to_export)}")

    for tool_name in tools_to_export:
        export_tool(tool_name, data, all_candidates, evaluations, model_dir)


if __name__ == "__main__":
    main()
