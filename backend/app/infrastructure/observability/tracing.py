from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token

from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context

INSTRUMENTATION_NAME = "codesage.agent-os"
_evaluation_context: ContextVar[dict[str, object]] = ContextVar("codesage_evaluation_context", default={})


def bind_evaluation_context(**values: object) -> Token:
    merged = dict(_evaluation_context.get())
    merged.update({key: value for key, value in values.items() if value is not None})
    return _evaluation_context.set(merged)


def reset_evaluation_context(token: Token) -> None:
    _evaluation_context.reset(token)


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
    merged = {**_evaluation_context.get(), **values}
    return {
        f"codesage.{key}": value
        for key, value in merged.items()
        if value is not None and isinstance(value, (str, bool, int, float))
    }
