from __future__ import annotations

import random


REQUIRED_EQUAL_FIELDS = (
    "case_ids",
    "dataset_sha256",
    "judge_fingerprint",
    "scoring_version",
    "serializer_version",
    "coverage_version",
    "fixture_fingerprints",
)


def validate_comparable(baseline: dict, candidate: dict) -> list[str]:
    mismatches = [field for field in REQUIRED_EQUAL_FIELDS if baseline.get(field) != candidate.get(field)]
    return mismatches


def performance_comparison_mode(baseline: dict, candidate: dict) -> str:
    fields = ("concurrency", "timeout_seconds", "hardware_fingerprint", "budget")
    return "comparable" if all(baseline.get(key) == candidate.get(key) for key in fields) else "descriptive_only"


def paired_bootstrap(
    baseline_by_case: dict[str, float],
    candidate_by_case: dict[str, float],
    *,
    seed: int = 20260909,
    iterations: int = 1000,
) -> dict:
    case_ids = sorted(set(baseline_by_case) & set(candidate_by_case))
    if not case_ids:
        raise ValueError("paired comparison has no shared cases")
    rng = random.Random(seed)
    deltas = []
    for _ in range(iterations):
        sample = [rng.choice(case_ids) for _ in case_ids]
        deltas.append(
            sum(candidate_by_case[item] - baseline_by_case[item] for item in sample) / len(sample)
        )
    deltas.sort()
    return {
        "seed": seed,
        "iterations": iterations,
        "case_count": len(case_ids),
        "delta_mean": sum(deltas) / len(deltas),
        "delta_ci95": [deltas[int(iterations * 0.025)], deltas[min(iterations - 1, int(iterations * 0.975))]],
    }
