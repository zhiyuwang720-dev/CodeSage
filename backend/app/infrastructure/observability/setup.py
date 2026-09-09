from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

from app.infrastructure.observability.exporters import (
    OtlpJsonlMetricExporter,
    OtlpJsonlSpanExporter,
    SafeSpanExporter,
)

logger = logging.getLogger(__name__)


class TraceCorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        current = trace.get_current_span().get_span_context()
        record.otelTraceID = format(current.trace_id, "032x") if current.is_valid else "0" * 32
        record.otelSpanID = format(current.span_id, "016x") if current.is_valid else "0" * 16
        return True


@dataclass
class ObservabilityRuntime:
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None

    def force_flush(self, timeout_millis: int = 5000) -> bool:
        if self.tracer_provider is None:
            return True
        try:
            return bool(self.tracer_provider.force_flush(timeout_millis))
        except Exception:
            logger.warning("OpenTelemetry flush failed", exc_info=True)
            return False

    def shutdown(self) -> None:
        if self.tracer_provider is not None:
            try:
                self.tracer_provider.shutdown()
            except Exception:
                logger.warning("OpenTelemetry trace shutdown failed", exc_info=True)
        if self.meter_provider is not None:
            try:
                self.meter_provider.shutdown(timeout_millis=5000)
            except Exception:
                logger.warning("OpenTelemetry metrics shutdown failed", exc_info=True)


def configure_observability(
    *,
    service_name: str,
    enabled: bool,
    endpoint: str | None = None,
    local_trace_path: str | Path | None = None,
    local_metric_path: str | Path | None = None,
    export_timeout_seconds: float = 3.0,
    max_attribute_bytes: int = 8192,
) -> ObservabilityRuntime:
    """Process bootstrap entry point. Library modules must not call this function."""
    if not enabled:
        return ObservabilityRuntime()
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
            "deployment.environment.name": os.getenv("CODESAGE_ENV", "local"),
            "openinference.project.name": os.getenv("OTEL_PROJECT_NAME", "codesage"),
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    if local_trace_path:
        tracer_provider.add_span_processor(
            SimpleSpanProcessor(
                SafeSpanExporter(
                    OtlpJsonlSpanExporter(local_trace_path), max_bytes=max_attribute_bytes
                )
            )
        )
    if endpoint:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                SafeSpanExporter(
                    OTLPSpanExporter(endpoint=endpoint, timeout=export_timeout_seconds),
                    max_bytes=max_attribute_bytes,
                )
            )
        )
    trace.set_tracer_provider(tracer_provider)
    readers = []
    if local_metric_path:
        readers.append(
            PeriodicExportingMetricReader(
                OtlpJsonlMetricExporter(local_metric_path), export_interval_millis=5000
            )
        )
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(meter_provider)
    correlation_filter = TraceCorrelationFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(correlation_filter)
    return ObservabilityRuntime(tracer_provider, meter_provider)
