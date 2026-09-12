"""Versioned pricing catalog and deterministic reconciliation helpers."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

PRICE_STATUS_OK = "ok"
PRICE_STATUS_UNKNOWN = "price_unknown"
PRICE_STATUS_CONFLICT = "pricing_conflict"
PRICE_STATUS_UNSUPPORTED = "unsupported"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PriceCatalogEntry:
    schema_version: int
    price_id: str
    provider: str
    endpoint_id: str
    model: str
    aliases: tuple[str, ...]
    currency: str
    effective_from: str
    effective_to: str | None
    input_per_million: str
    cache_read_per_million: str
    cache_write_per_million: str
    output_per_million: str
    source: str
    version: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PriceCatalogEntry":
        required = (
            "price_id",
            "provider",
            "endpoint_id",
            "model",
            "currency",
            "effective_from",
            "input_per_million",
            "output_per_million",
            "source",
            "version",
        )
        missing = [key for key in required if value.get(key) in (None, "")]
        if missing:
            raise ValueError(f"price entry missing fields: {', '.join(missing)}")
        return cls(
            schema_version=int(value.get("schema_version") or 1),
            price_id=str(value["price_id"]),
            provider=str(value["provider"]),
            endpoint_id=str(value["endpoint_id"]),
            model=str(value["model"]),
            aliases=tuple(str(item) for item in (value.get("aliases") or [])),
            currency=str(value["currency"]).upper(),
            effective_from=str(value["effective_from"]),
            effective_to=str(value["effective_to"]) if value.get("effective_to") else None,
            input_per_million=str(value["input_per_million"]),
            cache_read_per_million=str(value.get("cache_read_per_million") or "0"),
            cache_write_per_million=str(value.get("cache_write_per_million") or "0"),
            output_per_million=str(value["output_per_million"]),
            source=str(value["source"]),
            version=str(value["version"]),
        )


@dataclass(frozen=True)
class PricingResolution:
    status: str
    entry: PriceCatalogEntry | None = None
    reason: str | None = None
    matches: tuple[str, ...] = ()


@dataclass
class PricingCatalog:
    entries: list[PriceCatalogEntry] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "PricingCatalog":
        target = Path(path)
        lines = [line.strip() for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
        entries = [PriceCatalogEntry.from_mapping(json.loads(line)) for line in lines]
        return cls(entries=entries, raw_lines=lines)

    @property
    def price_hash(self) -> str:
        payload = "\n".join(self.raw_lines).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def resolve(self, *, provider: str, endpoint_id: str, model: str, at: datetime) -> PricingResolution:
        point = at.astimezone(timezone.utc)
        matches: list[PriceCatalogEntry] = []
        for entry in self.entries:
            if entry.provider.lower() != str(provider).lower() or entry.endpoint_id != endpoint_id:
                continue
            names = {entry.model.lower(), *(alias.lower() for alias in entry.aliases)}
            if str(model).lower() not in names:
                continue
            start = _parse_time(entry.effective_from)
            end = _parse_time(entry.effective_to)
            if start is not None and point < start:
                continue
            if end is not None and point >= end:
                continue
            matches.append(entry)
        if not matches:
            return PricingResolution(status=PRICE_STATUS_UNKNOWN, reason="no_exact_active_price")
        if len(matches) > 1:
            return PricingResolution(
                status=PRICE_STATUS_CONFLICT,
                reason="multiple_active_prices",
                matches=tuple(item.price_id for item in matches),
            )
        return PricingResolution(status=PRICE_STATUS_OK, entry=matches[0])

    @staticmethod
    def estimate_cost(entry: PriceCatalogEntry, usage: Mapping[str, Any]) -> tuple[Decimal | None, str | None]:
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if not isinstance(prompt, int) or not isinstance(completion, int):
            return None, "usage_incomplete"
        cache_read = usage.get("cache_read_tokens")
        cache_write = usage.get("cache_write_tokens")
        if entry.cache_read_per_million != "0" and cache_read is None:
            return None, "cache_read_missing"
        if entry.cache_write_per_million != "0" and cache_write is None:
            return None, "cache_write_missing"
        read = int(cache_read or 0)
        write = int(cache_write or 0)
        if read > prompt or write > prompt or read + write > prompt:
            return None, "cache_categories_exceed_prompt"
        uncached = prompt - read - write
        million = Decimal(1_000_000)
        cost = (
            Decimal(uncached) * Decimal(entry.input_per_million)
            + Decimal(read) * Decimal(entry.cache_read_per_million)
            + Decimal(write) * Decimal(entry.cache_write_per_million)
            + Decimal(completion) * Decimal(entry.output_per_million)
        ) / million
        return cost, None

    def doctor(self, *, provider: str, endpoint_id: str, model: str, at: datetime) -> dict[str, Any]:
        resolution = self.resolve(provider=provider, endpoint_id=endpoint_id, model=model, at=at)
        return {
            "status": resolution.status,
            "reason": resolution.reason,
            "price_id": resolution.entry.price_id if resolution.entry else None,
            "price_hash": self.price_hash,
            "matches": list(resolution.matches),
        }


def sync_catalog_to_file(catalog: PricingCatalog, target: str | Path) -> dict[str, Any]:
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(catalog.raw_lines) + ("\n" if catalog.raw_lines else "")
    path.write_text(content, encoding="utf-8")
    return {"status": "written", "target": str(path), "entries": len(catalog.entries), "price_hash": catalog.price_hash}


_catalog_cache: dict[str, tuple[float, int, PricingCatalog]] = {}
_catalog_lock = threading.Lock()


def load_pricing_catalog(path: str | Path | None) -> PricingCatalog | None:
    """Load a frozen JSONL catalog with mtime+size caching.

    Missing or unreadable catalogs return ``None`` so callers keep the cost
    value unknown instead of guessing. Bad rows also fail closed.
    """

    if not path:
        return None
    target = Path(path)
    if not target.is_absolute():
        target = Path.cwd() / target
    try:
        stat = target.stat()
    except OSError:
        return None
    key = str(target)
    with _catalog_lock:
        cached = _catalog_cache.get(key)
        if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
    try:
        catalog = PricingCatalog.from_jsonl(target)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    with _catalog_lock:
        _catalog_cache[key] = (stat.st_mtime, stat.st_size, catalog)
    return catalog


__all__ = [
    "PRICE_STATUS_CONFLICT",
    "PRICE_STATUS_OK",
    "PRICE_STATUS_UNKNOWN",
    "PRICE_STATUS_UNSUPPORTED",
    "PriceCatalogEntry",
    "PricingCatalog",
    "PricingResolution",
    "load_pricing_catalog",
    "sync_catalog_to_file",
]
