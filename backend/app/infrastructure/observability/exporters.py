from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Sequence

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.sdk.metrics.export import MetricExportResult, MetricExporter, MetricsData
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult, SpanExporter

from app.infrastructure.observability.privacy import sanitize

logger = logging.getLogger(__name__)


def _sanitized_span(span: ReadableSpan, *, max_bytes: int) -> ReadableSpan:
    events = tuple(
        Event(
            event.name,
            sanitize(dict(event.attributes or {}), max_bytes=max_bytes),
            event.timestamp,
        )
        for event in span.events
    )
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=sanitize(dict(span.attributes or {}), max_bytes=max_bytes),
        events=events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SafeSpanExporter(SpanExporter):
    """Redacts before export and isolates telemetry failures from product execution."""

    def __init__(self, delegate: SpanExporter, *, max_bytes: int = 8192):
        self._delegate = delegate
        self._max_bytes = max_bytes

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            return self._delegate.export(
                tuple(_sanitized_span(span, max_bytes=self._max_bytes) for span in spans)
            )
        except Exception:
            logger.warning("OpenTelemetry span export failed", exc_info=True)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:
            logger.warning("OpenTelemetry exporter shutdown failed", exc_info=True)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self._delegate.force_flush(timeout_millis))
        except Exception:
            logger.warning("OpenTelemetry exporter flush failed", exc_info=True)
            return False


class OtlpJsonlSpanExporter(SpanExporter):
    """Append one standard OTLP ExportTraceServiceRequest JSON object per batch."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if not spans:
            return SpanExportResult.SUCCESS
        payload = MessageToDict(
            encode_spans(spans), preserving_proto_field_name=True, use_integers_for_enums=False
        )
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
            return SpanExportResult.SUCCESS
        except OSError:
            logger.warning("Local OTLP JSONL export failed: %s", self._path, exc_info=True)
            return SpanExportResult.FAILURE


class OtlpJsonlMetricExporter(MetricExporter):
    """Append standard OTLP ExportMetricsServiceRequest objects to JSONL."""

    def __init__(self, path: str | Path):
        super().__init__()
        self._path = Path(path)
        self._lock = threading.Lock()

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10000, **kwargs):
        del timeout_millis, kwargs
        payload = MessageToDict(
            encode_metrics(metrics_data),
            preserving_proto_field_name=True,
            use_integers_for_enums=False,
        )
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
            return MetricExportResult.SUCCESS
        except OSError:
            logger.warning("Local OTLP metrics export failed: %s", self._path, exc_info=True)
            return MetricExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 10000) -> bool:
        del timeout_millis
        return True

    def shutdown(self, timeout_millis: float = 30000, **kwargs) -> None:
        del timeout_millis, kwargs
