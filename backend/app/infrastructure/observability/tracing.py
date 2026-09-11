from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token

from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context

INSTRUMENTATION_NAME = "codesage.agent-os"
_correlation_context: ContextVar[dict[str, object]] = ContextVar("codesage_correlation_context", default={})


def bind_observability_context(**values: object) -> Token:
    merged = dict(_correlation_context.get())
    merged.update({key: value for key, value in values.items() if value is not None})
    return _correlation_context.set(merged)


def reset_observability_context(token: Token) -> None:
    _correlation_context.reset(token)


def get_observability_context() -> dict[str, object]:
    return dict(_correlation_context.get())


# Backward-compatible aliases: evaluation code used the same context before Plan 20.
bind_evaluation_context = bind_observability_context
reset_evaluation_context = reset_observability_context


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
    merged = {**_correlation_context.get(), **values}
    return {
        f"codesage.{key}": value
        for key, value in merged.items()
        if value is not None and isinstance(value, (str, bool, int, float))
    }
