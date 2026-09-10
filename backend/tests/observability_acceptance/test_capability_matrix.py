from __future__ import annotations

import json
from pathlib import Path


MATRIX_PATH = Path(__file__).with_name("capability-matrix.json")


def test_plan20_capability_matrix_has_primary_deepseek_routes() -> None:
    payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    routes = payload["routes"]

    assert payload["schema_version"] == 1
    assert any(
        route["provider"] == "deepseek"
        and route["protocol"] == "openai_chat"
        and route["adapter"] == "LiteLLMAdapter"
        for route in routes
    )
    assert any(
        route["provider"] == "deepseek"
        and route["protocol"] == "anthropic_messages"
        and route["adapter"] == "AnthropicAdapter"
        for route in routes
    )
    assert all(route["send_point"] for route in routes)
    assert all(route["payload_builder"] for route in routes)
    assert all(route["usage_sources"] for route in routes)
    assert all(route["acceptance_cases"] for route in routes)
