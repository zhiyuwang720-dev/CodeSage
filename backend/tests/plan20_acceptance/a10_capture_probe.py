"""Deterministic probe: real LiteLLM SDK path -> model_response content capture.

Runs in its own process so the global OTel provider is created with the
local JSONL exporter; it never touches a real provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from tests.observability_acceptance.fixture_server import FixtureServer, PlannedResponse, openai_completion


def _unwrap(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in (
        "stringValue",
        "string_value",
        "intValue",
        "int_value",
        "doubleValue",
        "double_value",
        "boolValue",
        "bool_value",
    ):
        if key in value:
            return value[key]
    node = value.get("arrayValue") or value.get("array_value")
    if isinstance(node, dict):
        return [_unwrap(item) for item in node.get("values", [])]
    return value


def _spans(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        for resource_span in payload.get("resource_spans", payload.get("resourceSpans", [])):
            for scope_span in resource_span.get(
                "scope_spans", resource_span.get("scopeSpans", [])
            ):
                for span in scope_span.get("spans", []):
                    yield span


def _attributes(span: dict) -> dict:
    return {item["key"]: _unwrap(item.get("value")) for item in span.get("attributes", [])}


async def _run(output: Path, run_id: str) -> dict:
    from app.execution_plane.models.client import reset_sdk_client
    from app.execution_plane.models.service import LLMService
    from app.infrastructure.observability import configure_observability
    from app.infrastructure.observability.content import configure_content_store
    from app.infrastructure.observability.litellm_integration import reset_integration_state
    from app.infrastructure.observability.tracing import bind_observability_context, reset_observability_context

    output.mkdir(parents=True, exist_ok=True)
    server = FixtureServer().start()
    server.set_default(
        "/v1/chat/completions",
        PlannedResponse(payload=openai_completion(content="capture-ok", prompt_tokens=9, completion_tokens=3)),
    )
    trace_path = output / "traces.otlp.jsonl"
    runtime = configure_observability(
        service_name="a10-capture-probe",
        enabled=True,
        local_trace_path=trace_path,
        capture_content=True,
    )
    store = configure_content_store(root=output / "content", enabled=True)
    service = LLMService(
        user_config={
            "llmConfig": {
                "llmProvider": "openai",
                "llmApiKey": "fixture-key",
                "llmModel": "fixture-model",
                "llmBaseUrl": server.base_url,
                "endpointProtocol": "openai_chat",
            },
            "otherConfig": {"llmConcurrency": 1, "llmGapMs": 0},
        }
    )
    token = bind_observability_context(review_run_id=run_id, task_id="a10-capture-probe")
    try:
        result = await service.chat_completion(messages=[{"role": "user", "content": "ping"}], purpose="a10")
        # LiteLLM fires the async success callback after acompletion returns; give it time
        # to create the litellm_request span before flushing the batch processor.
        await asyncio.sleep(0.5)
        runtime.force_flush(10000)
    finally:
        reset_observability_context(token)
        runtime.shutdown()
        server.stop()
        reset_integration_state()
        reset_sdk_client()

    llm_span = None
    for span in _spans(trace_path) or []:
        attrs = _attributes(span)
        if attrs.get("openinference.span.kind") == "LLM" or "litellm" in str(span.get("name", "")).lower():
            llm_span = {"name": span.get("name"), "attributes": attrs}
            break
    if llm_span is None:
        raise RuntimeError("no LLM span exported")

    attrs = llm_span["attributes"]
    artifact_verified = None
    relative = attrs.get("codesage.model_response.relative_path")
    if relative:
        artifact = store.find_artifact(run_id, Path(relative).stem)
        if artifact is not None:
            artifact_verified = len(store.read_verified(artifact))
    return {
        "run_id": run_id,
        "content": result.get("content"),
        "span_name": llm_span["name"],
        "model_request_capture_status": attrs.get("codesage.model_request.capture_status"),
        "model_response_capture_status": attrs.get("codesage.model_response.capture_status"),
        "model_response_reason": attrs.get("codesage.model_response.reason"),
        "output_value": attrs.get("output.value"),
        "response_artifact_relative_path": relative,
        "response_artifact_verified_bytes": artifact_verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    payload = asyncio.run(_run(Path(args.output), args.run_id))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
