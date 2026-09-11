"""A20-A22: metrics aggregation, counter reset, cohort, and generation rate."""

from __future__ import annotations

import json

from app.infrastructure.observability.metrics import (
    counter_delta,
    nearest_rank_percentile,
    output_generation_rate,
    task_completion_rate,
    token_rate,
)


def test_a20_counter_snapshot_diff_and_resource_restart() -> None:
    first = {"resource_instance": "pid-1", "start_time_unix_nano": 1, "values": {"tokens": 0}}
    second = {"resource_instance": "pid-1", "start_time_unix_nano": 1, "values": {"tokens": 600}}
    third = {"resource_instance": "pid-1", "start_time_unix_nano": 1, "values": {"tokens": 1200}}
    assert counter_delta(first, second) == {"tokens": 600.0}
    assert token_rate(second, third, 30) == 20.0
    restarted = {"resource_instance": "pid-2", "start_time_unix_nano": 2, "values": {"tokens": 10}}
    assert counter_delta(third, restarted) == {}


def test_a20_parallel_durations_do_not_sum_to_wall_clock() -> None:
    wall_clock = 10.0
    parallel_durations = [7.0, 6.0, 5.0]
    assert max(parallel_durations) <= wall_clock
    assert nearest_rank_percentile(parallel_durations, 0.95) == 7.0


def test_a21_cohort_completion_rate_uses_latest_state() -> None:
    cohort = {"a": "completed", "b": "completed", "c": "failed", "d": "running"}
    before = task_completion_rate(cohort)
    assert before["submitted"] == 4
    assert before["completed"] == 0.5
    cohort["c"] = "completed"
    after = task_completion_rate(cohort)
    assert after["submitted"] == 4
    assert after["completed"] == 0.75


def test_a22_output_generation_rate_only_for_stream_deltas() -> None:
    assert output_generation_rate(100, None, None, delta_count=0) is None
    assert output_generation_rate(100, 1.0, 2.0, delta_count=1) is None
    assert output_generation_rate(100, None, 2.0, delta_count=3) is None
    assert output_generation_rate(100, 1.0, 3.0, delta_count=3) == 50.0


def test_metrics_evidence(acceptance_artifact_root) -> None:
    payload = {
        "rate": token_rate(
            {"resource_instance": "pid-1", "start_time_unix_nano": 1, "values": {"tokens": 0}},
            {"resource_instance": "pid-1", "start_time_unix_nano": 1, "values": {"tokens": 600}},
            30,
        ),
        "cohort": task_completion_rate({"a": "completed", "b": "failed", "c": "running"}),
        "generation_rate": output_generation_rate(100, 1.0, 3.0, delta_count=3),
        "percentile": nearest_rank_percentile([1, 2, 3, 4], 0.5),
    }
    target = acceptance_artifact_root / "evidence" / "a20" / "metrics.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
