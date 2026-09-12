"""Deterministic A29 benchmark: real SDK + local HTTP fixture."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from tests.observability_acceptance.fixture_server import FixtureServer, PlannedResponse, openai_completion


async def _run(observability: str, output: Path, calls: int) -> dict:
    from app.execution_plane.models.client import reset_sdk_client
    from app.execution_plane.models.service import LLMService
    from app.infrastructure.observability import configure_observability
    from app.infrastructure.observability.litellm_integration import reset_integration_state

    output.mkdir(parents=True, exist_ok=True)
    server = FixtureServer().start()
    enabled = observability == "on"
    runtime = configure_observability(
        service_name=f"a29-{observability}",
        enabled=enabled,
        local_trace_path=output / "traces.otlp.jsonl" if enabled else None,
        local_metric_path=output / "metrics.otlp.jsonl" if enabled else None,
        capture_content=False,
    )
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
    server.set_default("/v1/chat/completions", PlannedResponse(payload=openai_completion(content="ok")))
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    try:
        contents = []
        for _ in range(calls):
            result = await service.chat_completion(messages=[{"role": "user", "content": "ping"}], purpose="a29")
            contents.append(result["content"])
        runtime.force_flush(5000)
        wall = time.perf_counter() - wall_start
        cpu = time.process_time() - cpu_start
        trace_path = output / "traces.otlp.jsonl"
        metric_path = output / "metrics.otlp.jsonl"
        return {
            "observability": observability,
            "calls": calls,
            "content": contents[-1],
            "wall_seconds": wall,
            "cpu_seconds": cpu,
            "http_requests": len(server.requests()),
            "trace_bytes": trace_path.stat().st_size if enabled and trace_path.exists() else 0,
            "metric_bytes": metric_path.stat().st_size if enabled and metric_path.exists() else 0,
        }
    finally:
        runtime.shutdown()
        server.stop()
        reset_integration_state()
        reset_sdk_client()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observability", choices=("on", "off"), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--calls", type=int, default=20)
    args = parser.parse_args()
    result = asyncio.run(_run(args.observability, Path(args.output), args.calls))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
