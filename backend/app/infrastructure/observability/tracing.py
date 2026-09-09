from __future__ import annotations

from collections.abc import Mapping

from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context

INSTRUMENTATION_NAME = "codesage.agent-os"


def get_tracer():
    return trace.get_tracer(INSTRUMENTATION_NAME)


def get_meter():
    return metrics.get_meter(INSTRUMENTATION_NAME)


def inject_trace_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {key: carrier[key] for key in ("traceparent", "tracestate") if carrier.get(key)}


def extract_trace_context(carrier: Mapping[str, str] | None) -> Context:
    allowed = {
        key: str(value)
        for key, value in dict(carrier or {}).items()
        if key.lower() in {"traceparent", "tracestate"}
    }
    return propagate.extract(allowed)


def span_attributes(**values: object) -> dict[str, object]:
    return {
        f"codesage.{key}": value
        for key, value in values.items()
        if value is not None and isinstance(value, (str, bool, int, float))
    }
