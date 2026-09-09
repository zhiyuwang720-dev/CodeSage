from __future__ import annotations

import pytest

from codesage_eval.contracts import CandidateFinding, GoldenFinding, JudgmentRecord
from codesage_eval.scoring import score_case


def _gold(golden_id, *, severity=None):
    return GoldenFinding(golden_id=golden_id, comment=golden_id, severity=severity)


def _candidate(candidate_id, **kwargs):
    return CandidateFinding(candidate_id=candidate_id, title=candidate_id, **kwargs)


def _edge(candidate_id, golden_id, confidence=1.0, matched=True):
    return JudgmentRecord(
        eval_run_id="run", case_id="case", golden_id=golden_id, candidate_id=candidate_id,
        matched=matched, confidence=confidence, judge_fingerprint="judge", cache_key=f"{candidate_id}-{golden_id}",
    )


def test_one_to_many_uses_one_to_one_primary_but_coverage_exposes_breadth():
    score = score_case([_gold("g1"), _gold("g2")], [_candidate("c1")], [_edge("c1", "g1"), _edge("c1", "g2")])
    assert (score["tp"], score["fp"], score["fn"]) == (1, 0, 1)
    assert score["golden_coverage"] == 1


def test_many_to_one_duplicate_candidates_leave_closed_set_fp():
    candidates = [_candidate("c1", description="same"), _candidate("c2", description="same")]
    score = score_case([_gold("g1")], candidates, [_edge("c1", "g1"), _edge("c2", "g1")])
    assert (score["tp"], score["fp"], score["fn"]) == (1, 1, 0)


def test_confidence_then_stable_id_breaks_matching_ties():
    edges = [_edge("c1", "g1", .2), _edge("c1", "g2", .9), _edge("c2", "g1", .8), _edge("c2", "g2", .1)]
    score = score_case([_gold("g1"), _gold("g2")], [_candidate("c1"), _candidate("c2")], edges)
    assert score["pairs"] == [{"candidate_id": "c1", "golden_id": "g2"}, {"candidate_id": "c2", "golden_id": "g1"}]


@pytest.mark.parametrize(
    ("golden", "candidates", "expected"),
    [([_gold("g")], [], (0, 0, 1)), ([], [_candidate("c")], (0, 1, 0)), ([], [], (0, 0, 0))],
)
def test_empty_sides_and_metric_bounds(golden, candidates, expected):
    score = score_case(golden, candidates, [])
    assert (score["tp"], score["fp"], score["fn"]) == expected
    for metric in ("precision", "recall", "f1", "f2", "golden_coverage", "high_severity_recall"):
        assert 0 <= score[metric] <= 1
