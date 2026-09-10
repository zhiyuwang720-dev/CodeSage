from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from .types import LLMUsage

FieldSource = Literal["provider", "derived", "missing"]

_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        dumped = as_dict()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        return dict(attributes)
    return {}


def _integer(
    raw: Mapping[str, Any],
    key: str,
    *,
    field: str,
    anomalies: list[str],
) -> tuple[int | None, FieldSource]:
    if key not in raw or raw[key] is None:
        return None, "missing"
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        anomalies.append(f"{field}:invalid_integer")
        return None, "missing"
    if value < 0:
        anomalies.append(f"{field}:negative")
        return None, "missing"
    return value, "provider"


def _nested_integer(
    raw: Mapping[str, Any],
    container: str,
    key: str,
    *,
    field: str,
    anomalies: list[str],
) -> tuple[int | None, FieldSource]:
    nested = _mapping(raw.get(container))
    return _integer(nested, key, field=field, anomalies=anomalies)


def normalize_usage(
    raw_usage: Mapping[str, Any] | Any | None,
    *,
    provider: str,
    protocol: str | None = None,
    zero_fidelity: Literal["provider", "unverified"] = "provider",
    estimated_input_tokens: int | None = None,
    estimated_output_tokens: int | None = None,
    estimation_method: str = "tiktoken",
    estimation_version: str = "1",
) -> LLMUsage | None:
    """Normalize provider usage while preserving provenance and malformed raw facts.

    ``zero_fidelity=unverified`` is for SDKs known to synthesize an all-zero usage
    object when the wire response omitted usage. Estimates remain in a separate
    namespace and never populate provider token fields.
    """

    if raw_usage is None:
        return None
    raw = _mapping(raw_usage)
    if not raw:
        return None
    anomalies: list[str] = []
    normalized_provider = str(provider or "").strip().lower()
    normalized_protocol = str(protocol or "").strip().lower()

    if normalized_protocol == "gemini_native" or normalized_provider == "gemini":
        prompt, prompt_source = _integer(raw, "promptTokenCount", field="prompt_tokens", anomalies=anomalies)
        completion, completion_source = _integer(raw, "candidatesTokenCount", field="completion_tokens", anomalies=anomalies)
        total, total_source = _integer(raw, "totalTokenCount", field="total_tokens", anomalies=anomalies)
        cache_read, cache_read_source = _integer(raw, "cachedContentTokenCount", field="cache_read_tokens", anomalies=anomalies)
        reasoning, reasoning_source = _integer(raw, "thoughtsTokenCount", field="reasoning_tokens", anomalies=anomalies)
        cache_write, cache_write_source = None, "missing"
    elif normalized_protocol == "anthropic_messages" or normalized_provider in {"anthropic", "claude"}:
        uncached, uncached_source = _integer(raw, "input_tokens", field="input_tokens", anomalies=anomalies)
        cache_read, cache_read_source = _integer(raw, "cache_read_input_tokens", field="cache_read_tokens", anomalies=anomalies)
        cache_write, cache_write_source = _integer(raw, "cache_creation_input_tokens", field="cache_write_tokens", anomalies=anomalies)
        completion, completion_source = _integer(raw, "output_tokens", field="completion_tokens", anomalies=anomalies)
        reasoning, reasoning_source = _integer(raw, "reasoning_tokens", field="reasoning_tokens", anomalies=anomalies)
        known_input_parts = [value for value in (uncached, cache_read, cache_write) if value is not None]
        if uncached is not None and len(known_input_parts) == 3:
            prompt = sum(known_input_parts)
            prompt_source = "derived"
        else:
            prompt = uncached
            prompt_source = uncached_source
        total, total_source = _integer(raw, "total_tokens", field="total_tokens", anomalies=anomalies)
    else:
        prompt, prompt_source = _integer(raw, "prompt_tokens", field="prompt_tokens", anomalies=anomalies)
        if prompt is None:
            prompt, prompt_source = _integer(raw, "input_tokens", field="prompt_tokens", anomalies=anomalies)
        completion, completion_source = _integer(raw, "completion_tokens", field="completion_tokens", anomalies=anomalies)
        if completion is None:
            completion, completion_source = _integer(raw, "output_tokens", field="completion_tokens", anomalies=anomalies)
        total, total_source = _integer(raw, "total_tokens", field="total_tokens", anomalies=anomalies)
        cache_read, cache_read_source = _nested_integer(
            raw, "prompt_tokens_details", "cached_tokens", field="cache_read_tokens", anomalies=anomalies
        )
        if cache_read is None:
            cache_read, cache_read_source = _integer(raw, "prompt_cache_hit_tokens", field="cache_read_tokens", anomalies=anomalies)
        cache_write, cache_write_source = _integer(raw, "cache_creation_input_tokens", field="cache_write_tokens", anomalies=anomalies)
        reasoning, reasoning_source = _nested_integer(
            raw, "completion_tokens_details", "reasoning_tokens", field="reasoning_tokens", anomalies=anomalies
        )

        if normalized_provider == "deepseek":
            cache_miss, _ = _integer(raw, "prompt_cache_miss_tokens", field="prompt_cache_miss_tokens", anomalies=anomalies)
            if prompt is None and cache_read is not None and cache_miss is not None:
                prompt = cache_read + cache_miss
                prompt_source = "derived"
            elif prompt is not None and cache_read is not None and cache_miss is not None and cache_read + cache_miss != prompt:
                anomalies.append("prompt_tokens:cache_parts_mismatch")

    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
        total_source = "derived"
    elif total is not None and prompt is not None and completion is not None and total != prompt + completion:
        anomalies.append("total_tokens:sum_mismatch")

    values = (prompt, completion, total, cache_read, cache_write, reasoning)
    if zero_fidelity == "unverified" and raw and all(value == 0 for value in values if value is not None) and any(
        value is not None for value in values
    ):
        anomalies.append("usage:synthetic_zero_unverified")
        prompt = completion = total = cache_read = cache_write = reasoning = None
        prompt_source = completion_source = total_source = "missing"
        cache_read_source = cache_write_source = reasoning_source = "missing"

    estimated_usage = None
    if estimated_input_tokens is not None or estimated_output_tokens is not None:
        estimated_usage = {
            "input_tokens": estimated_input_tokens,
            "output_tokens": estimated_output_tokens,
            "total_tokens": (
                estimated_input_tokens + estimated_output_tokens
                if estimated_input_tokens is not None and estimated_output_tokens is not None
                else None
            ),
            "method": estimation_method,
            "version": estimation_version,
        }

    field_sources = dict(
        zip(
            _FIELDS,
            (
                prompt_source,
                completion_source,
                total_source,
                cache_read_source,
                cache_write_source,
                reasoning_source,
            ),
            strict=True,
        )
    )
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
        field_sources=field_sources,
        estimated_usage=estimated_usage,
        raw_usage=raw,
        anomalies=anomalies,
        usage_present=bool(raw),
    )
