"""Tests for step5_label_prs module."""

from __future__ import annotations

import argparse
import asyncio
import json

from code_review_benchmark import step5_label_prs as step5


def test_derive_language():
    entry = {"golden_source_file": "sentry.json"}
    assert step5.derive_language(entry) == "Python"


def test_derive_num_files_touched():
    entry = {
        "reviews": [
            {"review_comments": [{"path": "a.py"}, {"path": "b.py"}]},
            {"review_comments": [{"path": "a.py"}]},
        ]
    }
    assert step5.derive_num_files_touched(entry) == 2


def test_derive_severity_mix():
    golden_comments = [
        {"severity": "High"},
        {"severity": "Low"},
        {"severity": "High"},
    ]
    assert step5.derive_severity_mix(golden_comments) == {"High": 2, "Low": 1}


def test_process_batch_batches():
    async def identity(x):
        return x

    result = asyncio.run(step5.process_batch([identity(i) for i in range(5)], batch_size=2))
    assert result == [0, 1, 2, 3, 4]


def test_main_writes_labels(monkeypatch, tmp_path):
    monkeypatch.setenv("MARTIAN_MODEL", "label-model")
    monkeypatch.setenv("MARTIAN_API_KEY", "dummy")

    benchmark_data = {
        "https://example/pr": {
            "pr_title": "Fix issue",
            "golden_source_file": "sentry.json",
            "golden_comments": [
                {"comment": "Issue A", "severity": "High"},
                {"comment": "Issue B", "severity": "Low"},
            ],
            "reviews": [
                {
                    "review_comments": [{"path": "file.py"}]
                }
            ],
        }
    }

    results_dir = tmp_path
    benchmark_file = results_dir / "benchmark_data.json"
    benchmark_file.write_text(json.dumps(benchmark_data))

    labels_file = results_dir / "pr_labels.json"

    monkeypatch.setattr(step5, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(step5, "BENCHMARK_DATA_FILE", benchmark_file)
    monkeypatch.setattr(step5, "LABELS_FILE", labels_file)
    monkeypatch.setattr(step5, "load_dotenv", lambda: None)

    class DummyLabeler:
        def __init__(self):
            self.pr_calls: list[str] = []
            self.comment_calls: list[str] = []

        async def label_pr(self, **kwargs):
            self.pr_calls.append(kwargs.get("pr_title", ""))
            return {
                "bug_categories": ["logic_error"],
                "pr_size_category": "small",
                "domain": "API",
            }

        async def label_comment_bug_type(self, comment, severity="Unknown"):
            self.comment_calls.append((comment, severity))
            return {"bug_type": "logic_error", "reasoning": "clear"}

    labeler = DummyLabeler()

    monkeypatch.setattr(step5, "PRLabeler", lambda: labeler)

    class DummyParser:
        def __init__(self, *_, **__):
            pass

        def add_argument(self, *_, **__):
            return None

        def parse_args(self):
            return SimpleNamespace(force=False, limit=None)

    monkeypatch.setattr(argparse, "ArgumentParser", DummyParser)

    asyncio.run(step5.main())

    # Labels are now stored in central file, not per-model
    assert labels_file.exists()

    with labels_file.open() as fh:
        labels = json.load(fh)

    assert "https://example/pr" in labels
    pr_labels = labels["https://example/pr"]
    assert pr_labels["derived"]["language"] == "Python"
    assert pr_labels["llm_pr_labels"]["domain"] == "API"
    assert len(pr_labels["comment_bug_types"]) == 2


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
