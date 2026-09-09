from __future__ import annotations

import json

import pytest

from codesage_eval.contracts import CandidateFinding, DatasetCase, EvalCaseResult, GoldenFinding, JudgmentRecord
from codesage_eval.judge import judge_case
from codesage_eval.report import build_summary, write_report
from codesage_eval.storage import read_jsonl, write_jsonl_atomic


class FakeJudge:
    fingerprint = "judge-v1"

    def __init__(self, fails=False):
        self.calls = 0
        self.fails = fails

    def judge(self, golden, candidate):
        self.calls += 1
        if self.fails:
            raise TimeoutError("judge timeout")
        return True, 0.8, "same issue"


def _inputs():
    golden = GoldenFinding(golden_id="g1", comment="gold")
    candidate = CandidateFinding(candidate_id="c1", title="candidate")
    return golden, candidate


def test_judge_cache_marks_computed_then_reused(tmp_path):
    golden, candidate = _inputs()
    cache = tmp_path / "judge.jsonl"
    first = FakeJudge()
    assert judge_case(eval_run_id="r1", case_id="case", golden=[golden], candidates=[candidate], judge=first, cache_path=cache)[0].status == "computed"
    second = FakeJudge()
    record = judge_case(eval_run_id="r2", case_id="case", golden=[golden], candidates=[candidate], judge=second, cache_path=cache)[0]
    assert record.status == "reused"
    assert second.calls == 0


def test_unknown_is_not_cached_and_bad_jsonl_blocks_read(tmp_path):
    golden, candidate = _inputs()
    cache = tmp_path / "judge.jsonl"
    records = judge_case(eval_run_id="r", case_id="case", golden=[golden], candidates=[candidate], judge=FakeJudge(fails=True), cache_path=cache)
    assert records[0].status == "unknown"
    assert read_jsonl(cache) == []
    cache.write_text('{"ok":true}\nnot-json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        read_jsonl(cache)


def test_atomic_jsonl_unicode_round_trip(tmp_path):
    path = tmp_path / "data.jsonl"
    write_jsonl_atomic(path, [{"text": "中文"}, {"text": "🙂"}])
    assert read_jsonl(path) == [{"text": "中文"}, {"text": "🙂"}]


def test_report_keeps_missing_results_and_escapes_script_payload(tmp_path):
    case = DatasetCase(
        case_id="case</script><script>alert(1)</script>", pr_url="https://example.test", repo="中文 <repo>",
        pr_title="title", golden=[GoldenFinding(golden_id="g1", comment="gold")], golden_sha256="hash",
    )
    summary = build_summary([case], [], [])
    assert summary["case_count"] == 1
    assert summary["execution_completed"] == 0
    assert summary["effective_delivery"]["fn"] == 1
    output = write_report(tmp_path, summary, public=True)
    document = output.read_text(encoding="utf-8")
    assert "CodeSage Public Benchmark Report" in document
    assert "</script><script>alert(1)</script>" not in document
    assert "中文" in document
    assert str(tmp_path) not in document


def test_missing_pair_judgment_marks_quality_incomplete():
    golden, candidate = _inputs()
    case = DatasetCase(case_id="case", pr_url="url", repo="repo", pr_title="", golden=[golden], golden_sha256="hash")
    result = EvalCaseResult(eval_run_id="r", case_id="case", status="completed", execution_complete=True, candidates=[candidate])
    summary = build_summary([case], [result], [])
    assert summary["quality_completed"] == 0
    assert summary["cases"][0]["expected_judgments"] == 1
