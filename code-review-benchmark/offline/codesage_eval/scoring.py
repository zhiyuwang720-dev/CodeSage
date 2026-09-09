from __future__ import annotations

from collections.abc import Iterable

from codesage_eval.contracts import CandidateFinding, GoldenFinding, JudgmentRecord
from codesage_eval.matching import maximum_matching


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def score_case(
    golden: list[GoldenFinding],
    candidates: list[CandidateFinding],
    judgments: list[JudgmentRecord],
) -> dict:
    pairs = maximum_matching(judgments)
    paired_candidates = {candidate_id for candidate_id, _ in pairs}
    paired_golden = {golden_id for _, golden_id in pairs}
    tp = len(pairs)
    fp = len(candidates) - len(paired_candidates)
    fn = len(golden) - len(paired_golden)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)

    def f_score(beta: float) -> float:
        denominator = beta * beta * precision + recall
        return (1 + beta * beta) * precision * recall / denominator if denominator else 0.0

    known_edges = [item for item in judgments if item.matched is True]
    covered = {item.golden_id for item in known_edges}
    high_ids = {
        item.golden_id for item in golden if str(item.severity or "").lower() in {"critical", "high"}
    }
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f_score(1.0),
        "f2": f_score(2.0),
        "golden_coverage": _ratio(len(covered), len(golden)),
        "high_severity_recall": _ratio(len(high_ids & paired_golden), len(high_ids)),
        "pairs": [{"candidate_id": item[0], "golden_id": item[1]} for item in pairs],
        "unmatched_candidate_ids": sorted(item.candidate_id for item in candidates if item.candidate_id not in paired_candidates),
        "unmatched_golden_ids": sorted(item.golden_id for item in golden if item.golden_id not in paired_golden),
    }


def aggregate_micro(case_scores: Iterable[dict]) -> dict:
    scores = list(case_scores)
    tp = sum(int(item["tp"]) for item in scores)
    fp = sum(int(item["fp"]) for item in scores)
    fn = sum(int(item["fn"]) for item in scores)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    f2 = 5 * precision * recall / (4 * precision + recall) if 4 * precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1, "f2": f2}
