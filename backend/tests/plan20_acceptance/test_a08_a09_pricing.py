"""A08-A09: frozen catalog math, strict matching, and sync output."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.infrastructure.observability.pricing import (
    PRICE_STATUS_CONFLICT,
    PRICE_STATUS_OK,
    PRICE_STATUS_UNKNOWN,
    PricingCatalog,
    sync_catalog_to_file,
)


def _entry(price_id: str = "p1", *, effective_to: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "price_id": price_id,
        "provider": "deepseek",
        "endpoint_id": "fixture-endpoint",
        "model": "deepseek-chat",
        "aliases": ["deepseek-chat-alias"],
        "currency": "USD",
        "effective_from": "2026-01-01T00:00:00Z",
        "effective_to": effective_to,
        "input_per_million": "1",
        "cache_read_per_million": "0.1",
        "cache_write_per_million": "0",
        "output_per_million": "2",
        "source": "fixture-test-price",
        "version": "test-v1",
    }


def _catalog(tmp_path: Path, entries: list[dict]) -> PricingCatalog:
    path = tmp_path / "prices.jsonl"
    path.write_text("\n".join(json.dumps(item, sort_keys=True) for item in entries) + "\n", encoding="utf-8")
    return PricingCatalog.from_jsonl(path)


def test_a08_math_oracle_and_cache_categories_are_not_double_counted(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [_entry()])
    resolution = catalog.resolve(
        provider="deepseek",
        endpoint_id="fixture-endpoint",
        model="deepseek-chat",
        at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert resolution.status == PRICE_STATUS_OK
    assert resolution.entry is not None
    cost, reason = catalog.estimate_cost(
        resolution.entry,
        {"prompt_tokens": 1000, "cache_read_tokens": 400, "completion_tokens": 200},
    )
    assert reason is None
    assert cost == Decimal("0.00104")
    assert abs(float(cost) - 0.00104) <= 1e-9


def test_a09_effective_window_is_half_open_and_exact(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [_entry(effective_to="2026-06-01T00:00:00Z")])
    active = catalog.resolve(
        provider="deepseek",
        endpoint_id="fixture-endpoint",
        model="deepseek-chat-alias",
        at=datetime(2026, 5, 31, 23, 59, tzinfo=timezone.utc),
    )
    expired = catalog.resolve(
        provider="deepseek",
        endpoint_id="fixture-endpoint",
        model="deepseek-chat",
        at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    wrong_endpoint = catalog.resolve(
        provider="deepseek",
        endpoint_id="other-endpoint",
        model="deepseek-chat",
        at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    assert active.status == PRICE_STATUS_OK
    assert expired.status == PRICE_STATUS_UNKNOWN
    assert wrong_endpoint.status == PRICE_STATUS_UNKNOWN


def test_a09_overlapping_prices_are_conflict_not_guessed(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, [_entry("p1"), _entry("p2")])
    doctor = catalog.doctor(
        provider="deepseek",
        endpoint_id="fixture-endpoint",
        model="deepseek-chat",
        at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert doctor["status"] == PRICE_STATUS_CONFLICT
    assert doctor["reason"] == "multiple_active_prices"
    assert set(doctor["matches"]) == {"p1", "p2"}


def test_a09_sync_uses_same_frozen_catalog_hash(tmp_path: Path, acceptance_artifact_root) -> None:
    catalog = _catalog(tmp_path, [_entry()])
    target = tmp_path / "phoenix-prices.jsonl"
    result = sync_catalog_to_file(catalog, target)
    assert result["status"] == "written"
    assert result["price_hash"] == catalog.price_hash
    assert target.read_text(encoding="utf-8").strip() == catalog.raw_lines[0]

    evidence = acceptance_artifact_root / "evidence" / "a08" / "pricing.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({**result, "price_id": "p1"}), encoding="utf-8")
