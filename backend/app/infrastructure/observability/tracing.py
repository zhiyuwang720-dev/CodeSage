from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token

from opentelemetry import metrics, propagate, trace
from opentelemetry.trace import Status, StatusCode
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


_execution_span: ContextVar[object] = ContextVar("codesage_execution_span", default=None)


def set_execution_span(span: object):
    """Expose the worker execution.attempt span to code that claims the lease."""

    return _execution_span.set(span)


def reset_execution_span(token) -> None:
    _execution_span.reset(token)


def get_execution_span():
    return _execution_span.get()


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


def business_span(name: str, *, kind: str = "CHAIN"):
    """业务 span 装饰器：成功显式 OK，异常交给 OTel 记 ERROR（S20-T04）。"""

    def decorator(func):
        import functools

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            with get_tracer().start_as_current_span(
                name, attributes={"openinference.span.kind": kind}
            ) as span:
                result = await func(*args, **kwargs)
                mark_span_ok(span)
                return result

        return wrapper

    return decorator


def mark_span_ok(span: object) -> None:
    """成功显式 OK（S20-T04）。"""

    setter = getattr(span, "set_status", None)
    if callable(setter):
        setter(Status(StatusCode.OK))


def mark_span_error(span: object, description: str = "") -> None:
    """provider/工具错误即使未抛异常也标 ERROR（S20-T04）。"""

    setter = getattr(span, "set_status", None)
    if callable(setter):
        setter(Status(StatusCode.ERROR, description))


def mark_span_cancelled(span: object, description: str = "cancelled") -> None:
    """user cancel 保持 OTel UNSET，只记业务状态与事件（S20-T04）。"""

    setter = getattr(span, "set_attribute", None)
    if callable(setter):
        setter("codesage.status", "cancelled")
        setter("codesage.cancel_reason", description)


def mark_span_deferred(span: object, description: str = "deferred") -> None:
    """already_owned / ARQ Retry 不是执行失败（S20-T04）。"""

    setter = getattr(span, "set_attribute", None)
    if callable(setter):
        setter("codesage.status", description)


def span_attributes(**values: object) -> dict[str, object]:
    merged = {**_correlation_context.get(), **values}
    return {
        f"codesage.{key}": value
        for key, value in merged.items()
        if value is not None and isinstance(value, (str, bool, int, float))
    }
