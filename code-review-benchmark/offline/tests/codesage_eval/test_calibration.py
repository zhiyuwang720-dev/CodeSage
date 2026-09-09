from __future__ import annotations

from pathlib import Path

from codesage_eval.calibration import run_calibration
from codesage_eval.storage import read_jsonl


class HumanJudge:
    fingerprint = "fixed"

    def judge(self, golden, candidate):
        expected = not candidate.candidate_id.startswith(("no-", "extra-", "boundary-02", "multi-02", "duplicate-02"))
        return expected, 1.0, "fixture"


def test_fixed_calibration_has_twenty_pairs_and_two_uncached_runs():
    pairs = read_jsonl(Path(__file__).resolve().parents[2] / "codesage_eval/data/calibration_v1.jsonl")
    report = run_calibration(pairs, HumanJudge())
    assert report["pair_count"] == 20
    assert report["decided_pair_count"] == 20
    assert report["repeat_agreement"] == 1
    assert all(len(item["attempts"]) == 2 for item in report["pairs"])
