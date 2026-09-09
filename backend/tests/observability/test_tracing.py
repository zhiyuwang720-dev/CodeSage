from __future__ import annotations

import json

from opentelemetry import context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.infrastructure.observability.exporters import (
    OtlpJsonlMetricExporter,
    OtlpJsonlSpanExporter,
    SafeSpanExporter,
)
from app.infrastructure.observability.tracing import (
    bind_evaluation_context,
    extract_trace_context,
    inject_trace_context,
    reset_evaluation_context,
    span_attributes,
)


def build_provider(exporter: SpanExporter) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": "codesage-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def test_w3c_carrier_links_publish_and_worker_spans():
    exporter = InMemorySpanExporter()
    provider = build_provider(exporter)
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("task.publish") as parent:
        carrier = inject_trace_context()
        assert set(carrier) <= {"traceparent", "tracestate"}
        token = context.attach(extract_trace_context(carrier))
        try:
            with tracer.start_as_current_span("execution.attempt") as child:
                assert child.get_span_context().trace_id == parent.get_span_context().trace_id
        finally:
            context.detach(token)
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["execution.attempt", "task.publish"]
    assert spans[0].parent.span_id == spans[1].context.span_id


def test_evaluation_context_is_merged_and_reset():
    token = bind_evaluation_context(eval_run_id="run-1", case_id="case-1")
    try:
        assert span_attributes(task_id="task-1") == {
            "codesage.eval_run_id": "run-1",
            "codesage.case_id": "case-1",
            "codesage.task_id": "task-1",
        }
    finally:
        reset_evaluation_context(token)
    assert span_attributes(task_id="task-2") == {"codesage.task_id": "task-2"}


def test_safe_exporter_redacts_and_writes_standard_otlp_jsonl(tmp_path):
    path = tmp_path / "traces.otlp.jsonl"
    provider = build_provider(SafeSpanExporter(OtlpJsonlSpanExporter(path), max_bytes=32))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("provider.request") as span:
        span.set_attribute("authorization", "Bearer top-secret")
        span.set_attribute("request.url", "https://user:pass@example.test/v1")
        span.set_attribute("long.value", "x" * 100)
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    encoded = json.dumps(payload)
    assert "top-secret" not in encoded
    assert "user:pass" not in encoded
    assert "[REDACTED]" in encoded
    assert "resource_spans" in payload


class FailingExporter(SpanExporter):
    def export(self, spans):
        raise RuntimeError("collector unavailable")


def test_export_failure_never_escapes_product_span_end():
    provider = build_provider(SafeSpanExporter(FailingExporter()))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("result.commit"):
        pass
    assert provider.force_flush(1000) is True


def test_metrics_file_uses_standard_otlp_request_shape(tmp_path):
    path = tmp_path / "metrics.otlp.jsonl"
    reader = PeriodicExportingMetricReader(
        OtlpJsonlMetricExporter(path), export_interval_millis=60_000
    )
    provider = MeterProvider(metric_readers=[reader])
    provider.get_meter("test").create_counter("codesage.task.completed").add(
        1, {"status": "completed"}
    )
    assert provider.force_flush(1000) is True
    provider.shutdown(timeout_millis=1000)
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "resource_metrics" in payload
