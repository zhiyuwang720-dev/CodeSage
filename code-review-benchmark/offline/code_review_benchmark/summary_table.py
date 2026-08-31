#!/usr/bin/env python3
"""Generate summary table of reviews by tool and repo."""

from collections import defaultdict
import json
from pathlib import Path

RESULTS_DIR = Path("results")


def main():
    data_path = RESULTS_DIR / "benchmark_data.json"
    if not data_path.exists():
        print(f"Error: {data_path} not found")
        return

    with open(data_path) as f:
        data = json.load(f)

    # Count reviews per (tool, repo) pair
    # Use golden_source_file to determine repo category
    counts = defaultdict(lambda: defaultdict(int))
    tools = set()
    repos = set()

    for _url, entry in data.items():
        # Extract repo from golden_source_file (e.g., "sentry.json" -> "sentry")
        repo = entry["golden_source_file"].replace(".json", "")
        repos.add(repo)

        for review in entry.get("reviews", []):
            tool = review["tool"]
            tools.add(tool)
            # Count if there's at least one review comment
            if review.get("review_comments"):
                counts[tool][repo] += 1

    # Sort for consistent output
    tools = sorted(tools)
    repos = sorted(repos)

    # Calculate column widths
    tool_width = max(len(t) for t in tools) + 2
    col_width = max(len(r) for r in repos) + 2

    # Print header
    header = "Tool".ljust(tool_width) + "".join(r.ljust(col_width) for r in repos) + "Total"
    print(header)
    print("-" * len(header))

    # Print rows
    for tool in tools:
        row = tool.ljust(tool_width)
        total = 0
        for repo in repos:
            count = counts[tool][repo]
            total += count
            row += str(count).ljust(col_width)
        row += str(total)
        print(row)

    # Print totals row
    print("-" * len(header))
    totals_row = "Total".ljust(tool_width)
    grand_total = 0
    for repo in repos:
        repo_total = sum(counts[tool][repo] for tool in tools)
        grand_total += repo_total
        totals_row += str(repo_total).ljust(col_width)
    totals_row += str(grand_total)
    print(totals_row)


if __name__ == "__main__":
    main()
