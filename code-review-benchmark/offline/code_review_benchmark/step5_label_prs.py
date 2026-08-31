#!/usr/bin/env python3
"""
Label PRs with extra dimensions for analysis.

Derives language, complexity, and size from existing data.
Uses LLM to classify bug types and domains.

Labels are stored per-model in results/{model}/pr_labels.json.
"""

import asyncio
from collections import Counter
import json
import os
from pathlib import Path

from openai import AsyncOpenAI

RESULTS_DIR = Path("results")
BENCHMARK_DATA_FILE = RESULTS_DIR / "benchmark_data.json"
LABELS_FILE = RESULTS_DIR / "pr_labels.json"  # Central labels file (not per-model)
BATCH_SIZE = 40
LLM_CALL_TIMEOUT = 30

# Map source repo to language
REPO_LANGUAGE_MAP = {
    "keycloak": "Java",
    "sentry": "Python",
    "grafana": "Go",
    "discourse": "Ruby",
    "cal.com": "TypeScript",
    "calcom": "TypeScript",
    "cal_dot_com": "TypeScript",
}

BUG_TYPE_VALUES = [
    "logic_error",
    "null_reference",
    "race_condition",
    "type_error",
    "security",
    "dead_code",
    "missing_validation",
    "incorrect_value",
    "api_misuse",
    "resource_leak",
    "performance",
    "encoding",
    "boundary_check",
    "initialization",
    "other",
]

# New dimension values for filtering
CHANGE_TYPE_VALUES = ["bug_fix", "feature", "refactoring", "performance", "security_patch", "migration", "test_update"]
CODE_COMPLEXITY_VALUES = ["simple", "moderate", "complex"]
REVIEW_DIFFICULTY_VALUES = ["obvious", "moderate", "subtle", "very_subtle"]
RISK_LEVEL_VALUES = ["low", "medium", "high", "critical"]
REQUIRES_CONTEXT_VALUES = ["local", "file", "cross_file", "system"]
PRIMARY_CONCERN_VALUES = ["correctness", "security", "performance", "maintainability", "reliability"]

PR_LABEL_PROMPT = """You are labeling a pull request for analysis.
Given the PR title and the list of golden (expected) code review comments, provide structured labels.

PR Title: {pr_title}
Source Repo: {source_repo}
Number of files touched: {num_files}
Number of golden comments: {num_comments}

Golden Comments:
{golden_comments_text}

Provide labels as JSON:
{{
  "summary": "A brief 1-2 sentence description of what this PR does and what issues reviewers should catch",
  "bug_categories": ["list of bug types from: logic_error, null_reference, race_condition, \
type_error, security, dead_code, missing_validation, incorrect_value, api_misuse, resource_leak, \
performance, encoding, boundary_check, initialization, other"],
  "pr_size_category": "small/medium/large (small: 1-2 files & 1-2 comments, \
medium: 3-5 files or 3-4 comments, large: 6+ files or 5+ comments)",
  "domain": "one of: authentication, caching, UI, data_processing, API, networking, \
configuration, testing, logging, database, concurrency, error_handling, file_io, serialization, \
scheduling, memory_management, other",
  "change_type": "one of: bug_fix, feature, refactoring, performance, security_patch, migration, test_update",
  "code_complexity": "one of: simple, moderate, complex (based on logic depth, abstractions, dependencies)",
  "review_difficulty": "one of: obvious, moderate, subtle, very_subtle (how hard to spot the issues)",
  "risk_level": "one of: low, medium, high, critical (potential impact if bugs ship)",
  "requires_context": "one of: local, file, cross_file, system (how much context needed to review)",
  "primary_concern": "one of: correctness, security, performance, maintainability, reliability"
}}

Respond with ONLY the JSON object."""

COMMENT_BUG_TYPE_PROMPT = """Classify this code review comment into a single bug type category.

Comment: {comment}
Severity: {severity}

Categories: logic_error, null_reference, race_condition, type_error, security, dead_code,
missing_validation, incorrect_value, api_misuse, other

Respond with ONLY a JSON object:
{{"bug_type": "category_name", "reasoning": "brief explanation"}}"""


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


class PRLabeler:
    def __init__(self):
        load_dotenv()

        api_key = os.environ.get("MARTIAN_API_KEY")
        base_url = os.environ.get("MARTIAN_BASE_URL", "https://api.withmartian.com/v1")

        if not api_key:
            raise ValueError("MARTIAN_API_KEY environment variable required")

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = os.environ.get("MARTIAN_MODEL", "openai/gpt-4o-mini")

        print(f"Labeler model: {self.model}")
        print(f"Base URL: {base_url}")

    async def call_llm(self, prompt: str, max_retries: int = 3) -> dict:
        for attempt in range(max_retries):
            try:
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {
                                "role": "system",
                                "content": "You are a precise code review analyst. Always respond with valid JSON.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.0,
                    ),
                    timeout=LLM_CALL_TIMEOUT,
                )

                content = response.choices[0].message.content.strip()

                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()

                return json.loads(content)

            except TimeoutError:
                if attempt == max_retries - 1:
                    return {"error": f"Timed out after {LLM_CALL_TIMEOUT}s"}
                await asyncio.sleep(2**attempt)

            except json.JSONDecodeError:
                if attempt == max_retries - 1:
                    return {"error": "JSON parse failed"}
                await asyncio.sleep(1)

            except Exception as e:
                if attempt == max_retries - 1:
                    return {"error": str(e)}
                await asyncio.sleep(2**attempt)

        return {"error": "Max retries exceeded"}

    async def label_pr(self, pr_title: str, source_repo: str, golden_comments: list[dict], num_files: int) -> dict:
        golden_text = "\n".join(
            f"- [{gc.get('severity', 'Unknown')}] {gc['comment']}" for gc in golden_comments
        )

        prompt = PR_LABEL_PROMPT.format(
            pr_title=pr_title,
            source_repo=source_repo,
            num_files=num_files,
            num_comments=len(golden_comments),
            golden_comments_text=golden_text,
        )
        return await self.call_llm(prompt)

    async def label_comment_bug_type(self, comment: str, severity: str) -> dict:
        prompt = COMMENT_BUG_TYPE_PROMPT.format(comment=comment, severity=severity)
        return await self.call_llm(prompt)


async def process_batch(tasks: list, batch_size: int = BATCH_SIZE) -> list:
    results = []
    for i in range(0, len(tasks), batch_size):
        batch = tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        results.extend(batch_results)
        if i + batch_size < len(tasks):
            await asyncio.sleep(0.5)
    return results


def derive_language(entry: dict) -> str:
    """Derive programming language from source repo."""
    source_file = entry.get("golden_source_file", "")
    repo_name = source_file.replace(".json", "")
    return REPO_LANGUAGE_MAP.get(repo_name, "Unknown")


def derive_num_files_touched(entry: dict) -> int:
    """Count unique file paths across all review comments."""
    paths = set()
    for review in entry.get("reviews", []):
        for comment in review.get("review_comments", []):
            if comment.get("path"):
                paths.add(comment["path"])
    return len(paths)


def derive_severity_mix(golden_comments: list[dict]) -> dict:
    """Get distribution of severities."""
    counts = Counter(gc.get("severity", "Unknown") for gc in golden_comments)
    return dict(counts)


def derive_labels(entry: dict) -> dict:
    """Compute all non-LLM labels for a PR."""
    golden_comments = entry.get("golden_comments", [])
    return {
        "language": derive_language(entry),
        "num_golden_comments": len(golden_comments),
        "severity_mix": derive_severity_mix(golden_comments),
        "num_files_touched": derive_num_files_touched(entry),
    }


async def main():
    import argparse


    parser = argparse.ArgumentParser(description="Label PRs with extra dimensions for analysis")
    parser.add_argument("--force", action="store_true", help="Re-label even if already done")
    parser.add_argument("--limit", type=int, help="Limit number of PRs to label")
    args = parser.parse_args()

    load_dotenv()

    if not BENCHMARK_DATA_FILE.exists():
        print(f"Error: {BENCHMARK_DATA_FILE} not found")
        return

    with open(BENCHMARK_DATA_FILE) as f:
        benchmark_data = json.load(f)

    print(f"Loaded {len(benchmark_data)} PRs")

    # Use central labels file (labels are generated once, reused across models)
    labels_file = LABELS_FILE
    print(f"Labels file: {labels_file}")

    # Load existing labels
    if labels_file.exists() and not args.force:
        with open(labels_file) as f:
            all_labels = json.load(f)
        print(f"Loaded {len(all_labels)} existing labels")
    else:
        all_labels = {}
        if args.force:
            print("Force mode: re-labeling all PRs")

    # Phase 1: Derive non-LLM labels for all PRs
    print("\nPhase 1: Deriving labels from data...")
    for golden_url, entry in benchmark_data.items():
        if golden_url not in all_labels:
            all_labels[golden_url] = {}
        all_labels[golden_url]["derived"] = derive_labels(entry)

    # Phase 2: LLM-label PRs
    print("\nPhase 2: LLM labeling PRs...")
    labeler = PRLabeler()

    # Build work list for PR-level labels
    pr_work = []
    for golden_url, entry in benchmark_data.items():
        if not args.force and all_labels.get(golden_url, {}).get("llm_pr_labels"):
            continue
        pr_work.append((golden_url, entry))

    if args.limit:
        pr_work = pr_work[: args.limit]

    print(f"PRs needing LLM labels: {len(pr_work)}")

    if pr_work:
        pr_tasks = []
        pr_urls = []
        for golden_url, entry in pr_work:
            golden_comments = entry.get("golden_comments", [])
            num_files = derive_num_files_touched(entry)
            pr_tasks.append(
                labeler.label_pr(
                    pr_title=entry.get("pr_title", "Unknown"),
                    source_repo=entry.get("golden_source_file", "").replace(".json", ""),
                    golden_comments=golden_comments,
                    num_files=num_files,
                )
            )
            pr_urls.append(golden_url)

        pr_results = await process_batch(pr_tasks)

        pr_success = 0
        pr_errors = 0
        for idx, result in enumerate(pr_results):
            golden_url = pr_urls[idx]
            if isinstance(result, Exception):
                print(f"  Error for {golden_url}: {result}")
                pr_errors += 1
                continue
            if result.get("error"):
                print(f"  Error for {golden_url}: {result['error']}")
                pr_errors += 1
                continue

            all_labels[golden_url]["llm_pr_labels"] = result
            pr_success += 1

            if pr_success % 50 == 0:
                with open(labels_file, "w") as f:
                    json.dump(all_labels, f, indent=2)

        print(f"  PR labels: {pr_success} success, {pr_errors} errors")

    # Phase 3: LLM-label individual golden comments
    print("\nPhase 3: LLM labeling individual golden comments...")
    comment_work = []
    comment_meta = []

    for golden_url, entry in benchmark_data.items():
        golden_comments = entry.get("golden_comments", [])
        existing_bug_types = all_labels.get(golden_url, {}).get("comment_bug_types", [])

        if not args.force and len(existing_bug_types) == len(golden_comments) and existing_bug_types:
            continue

        for i, gc in enumerate(golden_comments):
            comment_work.append(
                labeler.label_comment_bug_type(gc["comment"], gc.get("severity", "Unknown"))
            )
            comment_meta.append((golden_url, i))

    if args.limit:
        comment_work = comment_work[: args.limit * 5]
        comment_meta = comment_meta[: args.limit * 5]

    print(f"Comments needing bug type labels: {len(comment_work)}")

    if comment_work:
        comment_results = await process_batch(comment_work)

        # Group results by PR
        pr_comment_results: dict[str, dict[int, dict]] = {}
        comment_success = 0
        comment_errors = 0

        for idx, result in enumerate(comment_results):
            golden_url, comment_idx = comment_meta[idx]
            if golden_url not in pr_comment_results:
                pr_comment_results[golden_url] = {}

            if isinstance(result, Exception):
                comment_errors += 1
                pr_comment_results[golden_url][comment_idx] = {"bug_type": "other", "error": str(result)}
                continue
            if result.get("error"):
                comment_errors += 1
                pr_comment_results[golden_url][comment_idx] = {"bug_type": "other", "error": result["error"]}
                continue

            pr_comment_results[golden_url][comment_idx] = result
            comment_success += 1

        # Assemble into ordered lists per PR
        for golden_url, results_map in pr_comment_results.items():
            num_comments = len(benchmark_data[golden_url].get("golden_comments", []))
            bug_types = []
            for i in range(num_comments):
                if i in results_map:
                    bug_types.append(results_map[i])
                else:
                    bug_types.append({"bug_type": "other", "error": "not_labeled"})
            all_labels[golden_url]["comment_bug_types"] = bug_types

        print(f"  Comment labels: {comment_success} success, {comment_errors} errors")

    # Save final results
    with open(labels_file, "w") as f:
        json.dump(all_labels, f, indent=2)

    print(f"\nDone! Labels saved to {labels_file}")

    # Print summary
    languages = Counter()
    domains = Counter()
    bug_types_counter = Counter()
    for _golden_url, labels in all_labels.items():
        derived = labels.get("derived", {})
        languages[derived.get("language", "Unknown")] += 1
        llm_labels = labels.get("llm_pr_labels", {})
        if llm_labels.get("domain"):
            domains[llm_labels["domain"]] += 1
        for bt in labels.get("comment_bug_types", []):
            bug_types_counter[bt.get("bug_type", "other")] += 1

    print(f"\nLanguages: {dict(languages)}")
    print(f"Domains: {dict(domains)}")
    print(f"Bug types: {dict(bug_types_counter)}")


if __name__ == "__main__":
    asyncio.run(main())
