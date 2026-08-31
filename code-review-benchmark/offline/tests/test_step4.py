"""Tests for step4_export_by_tool module."""

from __future__ import annotations

import argparse
import json

import openpyxl

from code_review_benchmark import step4_export_by_tool as step4


def test_main_writes_workbook(tmp_path, monkeypatch):
    monkeypatch.setenv("MARTIAN_MODEL", "judge-model")

    data = {
        "https://example/pr": {
            "original_url": "https://upstream/pr",
            "golden_comments": [{"comment": "Issue", "severity": "High"}],
            "reviews": [
                {
                    "tool": "tool-a",
                    "pr_url": "https://github.com/org/repo/pull/1",
                    "review_comments": [{"body": "Inline"}],
                }
            ],
        }
    }

    results_dir = tmp_path
    benchmark_file = results_dir / "benchmark_data.json"
    benchmark_file.write_text(json.dumps(data))

    candidates = {
        "https://example/pr": {
            "tool-a": [{"text": "Inline", "source": "line_comment"}]
        }
    }
    evaluations = {
        "https://example/pr": {
            "tool-a": {
                "tp": 1,
                "total_golden": 1,
                "true_positives": [
                    {
                        "golden_comment": "Issue",
                        "severity": "High",
                    }
                ],
                "false_negatives": [],
            }
        }
    }

    model_dir = results_dir / "judge-model"
    model_dir.mkdir()
    (model_dir / "candidates.json").write_text(json.dumps(candidates))
    (model_dir / "evaluations.json").write_text(json.dumps(evaluations))

    monkeypatch.setattr(step4, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(step4, "load_dotenv", lambda: None)

    class DummyParser:
        def __init__(self, *_, **__):
            pass

        def add_argument(self, *_, **__):
            return None

        def parse_args(self):
            return SimpleNamespace(tool="tool-a")

    monkeypatch.setattr(argparse, "ArgumentParser", DummyParser)

    step4.main()

    output_path = model_dir / "tool_exports" / "tool-a_reviews.xlsx"
    assert output_path.exists()

    workbook = openpyxl.load_workbook(output_path)
    sheet = workbook.active

    # Header + one data row
    assert sheet.max_row == 2
    assert sheet["A2"].value == "https://upstream/pr"
    assert "[FOUND]" in sheet["G2"].value


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
