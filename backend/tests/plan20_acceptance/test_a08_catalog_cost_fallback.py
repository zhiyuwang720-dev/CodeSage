"""A08: the frozen local catalog supplies cost when the SDK reports none."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import settings
from app.execution_plane.models.service import LLMService
from tests.observability_acceptance.fixture_server import FixtureServer, PlannedResponse, openai_completion


def _endpoint_id(base_url: str) -> str:
    parsed = urlsplit(base_url)
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _catalog(path: Path, endpoint_id: str, *, input_price: str, output_price: str, cache_read_price: str) -> Path:
    entry = {
        "schema_version": 1,
        "price_id": "fixture-price",
        "provider": "openai",
        "endpoint_id": endpoint_id,
        "model": "fixture-model",
        "aliases": [],
        "currency": "USD",
        "effective_from": "2026-01-01T00:00:00Z",
        "input_per_million": input_price,
        "cache_read_per_million": cache_read_price,
        "cache_write_per_million": "0",
        "output_per_million": output_price,
        "source": "fixture-test-price",
        "version": "test-v1",
    }
    path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _service(server: FixtureServer) -> LLMService:
    return LLMService(
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


def test_a08_catalog_cost_fallback_when_sdk_reports_none(
    tmp_path: Path, monkeypatch, acceptance_artifact_root
) -> None:
    server = FixtureServer().start()
    server.set_default(
        "/v1/chat/completions",
        PlannedResponse(
            payload=openai_completion(
                content="priced",
                prompt_tokens=1000,
                completion_tokens=200,
                extra_usage={"prompt_tokens_details": {"cached_tokens": 0}},
            )
        ),
    )
    catalog_path = _catalog(
        tmp_path / "prices.jsonl",
        _endpoint_id(server.base_url),
        input_price="0.27778",
        output_price="1.11111",
        cache_read_price="0.00556",
    )
    monkeypatch.setattr(settings, "PRICING_CATALOG_PATH", str(catalog_path))
    try:
        result = asyncio.run(
            _service(server).chat_completion(messages=[{"role": "user", "content": "ping"}], purpose="a08")
        )
    finally:
        server.stop()

    expected = 1000 * 0.27778 / 1_000_000 + 200 * 1.11111 / 1_000_000
    assert result["response_cost_usd"] is not None, result
    assert abs(result["response_cost_usd"] - expected) < 1e-12
    assert str(result["response_cost_source"]).startswith("frozen_catalog:fixture-price")

    target = acceptance_artifact_root / "evidence" / "a08" / "catalog_fallback.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "response_cost_usd": result["response_cost_usd"],
                "response_cost_source": result["response_cost_source"],
                "expected": expected,
                "usage": result["usage"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_a08_catalog_does_not_guess_when_model_unknown(tmp_path: Path, monkeypatch) -> None:
    catalog_path = _catalog(
        tmp_path / "prices.jsonl",
        "https://unknown.example",
        input_price="1",
        output_price="2",
        cache_read_price="0",
    )
    monkeypatch.setattr(settings, "PRICING_CATALOG_PATH", str(catalog_path))
    server = FixtureServer().start()
    server.set_default(
        "/v1/chat/completions",
        PlannedResponse(payload=openai_completion(content="unpriced", prompt_tokens=10, completion_tokens=5)),
    )
    try:
        result = asyncio.run(
            _service(server).chat_completion(messages=[{"role": "user", "content": "ping"}], purpose="a08")
        )
    finally:
        server.stop()
    assert result["response_cost_usd"] is None
    assert result["response_cost_source"] is None