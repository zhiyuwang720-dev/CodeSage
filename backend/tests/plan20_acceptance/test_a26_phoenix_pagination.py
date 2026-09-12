"""A26: real Phoenix cursor pagination and span deduplication."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from app.infrastructure.observability.tracing import bind_observability_context, reset_observability_context


pytestmark = pytest.mark.skipif(
    not os.getenv("CODESAGE_PHOENIX_URL"),
    reason="set CODESAGE_PHOENIX_URL to run real Phoenix pagination acceptance",
)


@pytest.mark.asyncio
async def test_a26_phoenix_pagination_deduplicates_all_spans(acceptance_artifact_root) -> None:
    phoenix_url = os.environ["CODESAGE_PHOENIX_URL"].rstrip("/")
    project = os.getenv("CODESAGE_PHOENIX_PROJECT", "codesage-product")
    run_id = f"a26-{uuid4().hex}"
    provider = TracerProvider(resource=Resource.create({"openinference.project.name": project}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"{phoenix_url}/v1/traces")))
    tracer = provider.get_tracer("codesage-a26")
    token = bind_observability_context(review_run_id=run_id, task_id="a26-task")
    try:
        for index in range(15):
            with tracer.start_as_current_span(f"a26.span.{index}") as span:
                span.set_attribute("codesage.review_run_id", run_id)
                span.set_attribute("codesage.index", index)
        provider.force_flush(10000)
    finally:
        reset_observability_context(token)
        provider.shutdown()

    client = httpx.Client(timeout=20.0, trust_env=False)
    unique: set[str] = set()
    pages = 0
    cursor = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            params = {"limit": 3, "attribute": f"codesage.review_run_id:{run_id}"}
            if cursor:
                params["cursor"] = cursor
            response = client.get(f"{phoenix_url}/v1/projects/{project}/spans", params=params)
            response.raise_for_status()
            body = response.json()
            for span in body.get("data", []):
                span_id = str((span.get("context") or {}).get("span_id") or span.get("id") or "")
                if span_id:
                    unique.add(span_id)
            pages += 1
            cursor = body.get("next_cursor")
            if not cursor and len(unique) >= 15:
                break
            time.sleep(0.25)
    finally:
        client.close()

    assert len(unique) >= 15, unique
    assert pages >= 5
    evidence = acceptance_artifact_root / "evidence" / "a26" / "phoenix_pagination.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps({"phoenix_url": phoenix_url, "project": project, "run_id": run_id, "pages": pages, "unique_spans": len(unique)}),
        encoding="utf-8",
    )
