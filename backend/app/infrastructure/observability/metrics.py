"""Central CodeSage instruments and deterministic telemetry aggregation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from opentelemetry.metrics import Counter, Histogram

from app.infrastructure.observability.tracing import get_meter


@dataclass(frozen=True)
class MetricInstruments:
    task_submissions: Counter
    task_terminal_transitions: Counter
    execution_attempts: Counter
    execution_lease_lost: Counter
    execution_recoveries: Counter
    harness_turns: Counter
    model_requests: Counter
    model_retries: Counter
    model_tokens: Counter
    tool_calls: Counter
    compactions: Counter
    telemetry_export_failures: Counter
    telemetry_dropped: Counter
    telemetry_truncated: Counter
    telemetry_usage_missing: Counter
    durations: dict[str, Histogram]


_instruments: MetricInstruments | None = None


def get_instruments() -> MetricInstruments:
    global _instruments
    if _instruments is not None:
        return _instruments
    meter = get_meter()
    durations = {
        name: meter.create_histogram(f"codesage.{name}.duration", unit="s")
        for name in ("queue", "claim", "execution", "perspective", "admission", "provider", "tool")
    }
    _instruments = MetricInstruments(
        task_submissions=meter.create_counter("codesage.task.submissions", unit="{operation}"),
        task_terminal_transitions=meter.create_counter("codesage.task.terminal_transitions", unit="{operation}"),
        execution_attempts=meter.create_counter("codesage.execution.attempts", unit="{operation}"),
        execution_lease_lost=meter.create_counter("codesage.execution.lease_lost", unit="{operation}"),
        execution_recoveries=meter.create_counter("codesage.execution.recoveries", unit="{operation}"),
        harness_turns=meter.create_counter("codesage.harness.turns", unit="{operation}"),
        model_requests=meter.create_counter("codesage.model.requests", unit="{operation}"),
        model_retries=meter.create_counter("codesage.model.retries", unit="{operation}"),
        model_tokens=meter.create_counter("codesage.model.tokens", unit="{token}"),
        tool_calls=meter.create_counter("codesage.tool.calls", unit="{operation}"),
        compactions=meter.create_counter("codesage.compactions", unit="{operation}"),
        telemetry_export_failures=meter.create_counter("codesage.telemetry.export_failures", unit="{operation}"),
        telemetry_dropped=meter.create_counter("codesage.telemetry.dropped", unit="{operation}"),
        telemetry_truncated=meter.create_counter("codesage.telemetry.truncated", unit="{operation}"),
        telemetry_usage_missing=meter.create_counter("codesage.telemetry.usage_missing", unit="{operation}"),
        durations=durations,
    )
    return _instruments


def reset_instruments_for_tests() -> None:
    global _instruments
    _instruments = None


def record_task_submission(*, status: str = "submitted") -> None:
    get_instruments().task_submissions.add(1, {"status": status})


def record_terminal_transition(*, status: str) -> None:
    get_instruments().task_terminal_transitions.add(1, {"status": status})


def record_execution_attempt(*, status: str) -> None:
    get_instruments().execution_attempts.add(1, {"status": status})


def record_model_request(*, provider: str, model: str, purpose: str, status: str) -> None:
    get_instruments().model_requests.add(
        1, {"provider": provider, "model": model, "purpose": purpose, "status": status}
    )


def record_model_retry(*, layer: str, error_kind: str) -> None:
    get_instruments().model_retries.add(1, {"layer": layer, "error_kind": error_kind})


def record_model_tokens(*, token_type: str, value: int, provider: str, model: str) -> None:
    if value > 0:
        get_instruments().model_tokens.add(value, {"token_type": token_type, "provider": provider, "model": model})


def record_tool_call(*, tool: str, status: str) -> None:
    get_instruments().tool_calls.add(1, {"tool": tool, "status": status})


def record_compaction(*, status: str) -> None:
    get_instruments().compactions.add(1, {"status": status})


def record_duration(*, name: str, seconds: float, attributes: Mapping[str, Any] | None = None) -> None:
    if name not in get_instruments().durations:
        raise KeyError(f"unknown duration instrument: {name}")
    if seconds >= 0:
        get_instruments().durations[name].record(seconds, dict(attributes or {}))


def record_telemetry(*, kind: str) -> None:
    mapping = {
        "export_failure": get_instruments().telemetry_export_failures,
        "dropped": get_instruments().telemetry_dropped,
        "truncated": get_instruments().telemetry_truncated,
        "usage_missing": get_instruments().telemetry_usage_missing,
    }
    counter = mapping.get(kind)
    if counter is None:
        raise KeyError(f"unknown telemetry metric: {kind}")
    counter.add(1, {"kind": kind})


def counter_delta(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, float]:
    if previous.get("resource_instance") != current.get("resource_instance"):
        return {}
    if previous.get("start_time_unix_nano") != current.get("start_time_unix_nano"):
        return {}
    result: dict[str, float] = {}
    for key, value in current.get("values", {}).items():
        before = previous.get("values", {}).get(key, 0)
        try:
            result[key] = max(0.0, float(value) - float(before))
        except (TypeError, ValueError):
            continue
    return result


def token_rate(previous: Mapping[str, Any], current: Mapping[str, Any], elapsed_seconds: float) -> float | None:
    if elapsed_seconds <= 0:
        return None
    delta = counter_delta(previous, current)
    if "tokens" not in delta:
        return None
    return delta["tokens"] / elapsed_seconds


def task_completion_rate(cohort: Mapping[str, str]) -> dict[str, float]:
    total = len(cohort)
    if total == 0:
        return {"completed": 0.0, "running": 0.0, "failed": 0.0, "cancelled": 0.0, "submitted": 0.0}
    counts = {"completed": 0, "running": 0, "failed": 0, "cancelled": 0}
    for status in cohort.values():
        if status in counts:
            counts[status] += 1
    result = {status: counts[status] / total for status in counts}
    result["submitted"] = float(total)
    return result


def output_generation_rate(
    output_tokens: int | None,
    first_content_delta: float | None,
    last_content_delta: float | None,
    *,
    delta_count: int,
) -> float | None:
    if output_tokens is None or delta_count < 2 or first_content_delta is None or last_content_delta is None:
        return None
    elapsed = last_content_delta - first_content_delta
    if elapsed <= 0:
        return None
    return output_tokens / elapsed


def nearest_rank_percentile(values: Iterable[float], percentile: float) -> float | None:
    samples = sorted(float(item) for item in values)
    if not samples:
        return None
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    index = max(0, math.ceil(percentile * len(samples)) - 1)
    return samples[index]


__all__ = [
    "MetricInstruments",
    "counter_delta",
    "get_instruments",
    "nearest_rank_percentile",
    "output_generation_rate",
    "record_compaction",
    "record_duration",
    "record_execution_attempt",
    "record_model_request",
    "record_model_retry",
    "record_model_tokens",
    "record_task_submission",
    "record_telemetry",
    "record_terminal_transition",
    "record_tool_call",
    "reset_instruments_for_tests",
    "task_completion_rate",
    "token_rate",
]
