from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from .types import LLMUsage

FieldSource = Literal["provider_raw", "sdk_normalized", "derived", "missing"]

NORMALIZATION_VERSION = "2"

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
    source: FieldSource = "provider_raw",
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
    return value, source


def _nested_integer(
    raw: Mapping[str, Any],
    container: str,
    key: str,
    *,
    field: str,
    anomalies: list[str],
    source: FieldSource = "provider_raw",
) -> tuple[int | None, FieldSource]:
    nested = _mapping(raw.get(container))
    return _integer(nested, key, field=field, anomalies=anomalies, source=source)


def normalize_usage(
    raw_usage: Mapping[str, Any] | Any | None,
    *,
    provider: str | None,
    protocol: str | None = None,
    zero_fidelity: Literal["provider", "unverified"] = "provider",
    estimated_input_tokens: int | None = None,
    estimated_output_tokens: int | None = None,
    estimation_method: str = "tiktoken",
    estimation_version: str = "1",
) -> LLMUsage | None:
    """Normalize SDK usage while preserving provenance and malformed raw facts.

    `provider` 只作为诊断信息保留，不再决定字段解析分支：SDK 已归一化的
    usage 不会被第二次套用厂商公式（避免缓存 token 被重复相加）。
    `zero_fidelity="unverified"` 用于 SDK 在 wire 无 usage 时合成全零对象的
    情况：全零被判为不可证实的合成零并保持 missing，而不是当成真实 0。
    """

    if raw_usage is None:
        return None
    raw = _mapping(raw_usage)
    if not raw:
        return None
    anomalies: list[str] = []
    source = "provider_raw" if _looks_like_provider_payload(raw) else "sdk_normalized"
    typed_source: FieldSource = source  # type: ignore[assignment]

    prompt, prompt_source = _integer(raw, "prompt_tokens", field="prompt_tokens", anomalies=anomalies, source=typed_source)
    if prompt is None:
        prompt, prompt_source = _integer(raw, "input_tokens", field="prompt_tokens", anomalies=anomalies, source=typed_source)

    completion, completion_source = _integer(
        raw, "completion_tokens", field="completion_tokens", anomalies=anomalies, source=typed_source
    )
    if completion is None:
        completion, completion_source = _integer(
            raw, "output_tokens", field="completion_tokens", anomalies=anomalies, source=typed_source
        )
    total, total_source = _integer(raw, "total_tokens", field="total_tokens", anomalies=anomalies, source=typed_source)

    cache_read, cache_read_source = _nested_integer(
        raw, "prompt_tokens_details", "cached_tokens", field="cache_read_tokens", anomalies=anomalies, source=typed_source
    )
    if cache_read is None:
        cache_read, cache_read_source = _integer(
            raw, "cache_read_input_tokens", field="cache_read_tokens", anomalies=anomalies, source=typed_source
        )
    if cache_read is None:
        cache_read, cache_read_source = _integer(
            raw, "prompt_cache_hit_tokens", field="cache_read_tokens", anomalies=anomalies, source=typed_source
        )

    cache_write, cache_write_source = _integer(
        raw, "cache_creation_input_tokens", field="cache_write_tokens", anomalies=anomalies, source=typed_source
    )

    reasoning, reasoning_source = _nested_integer(
        raw, "completion_tokens_details", "reasoning_tokens", field="reasoning_tokens", anomalies=anomalies, source=typed_source
    )
    if reasoning is None:
        reasoning, reasoning_source = _integer(
            raw, "reasoning_tokens", field="reasoning_tokens", anomalies=anomalies, source=typed_source
        )

    cache_miss = _integer(
        raw, "prompt_cache_miss_tokens", field="prompt_cache_miss_tokens", anomalies=anomalies, source=typed_source
    )[0]
    if prompt is None and cache_read is not None and cache_miss is not None:
        prompt = cache_read + cache_miss
        prompt_source = "derived"

    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
        total_source = "derived"
    elif total is not None and prompt is not None and completion is not None and total != prompt + completion:
        anomalies.append("total_tokens:sum_mismatch")

    values = (prompt, completion, total, cache_read, cache_write, reasoning)
    if zero_fidelity == "unverified" and all(value == 0 for value in values if value is not None) and any(
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
        usage_source=source,
        anomalies=anomalies,
        usage_present=bool(raw),
        normalization_version=NORMALIZATION_VERSION,
    )


def _looks_like_provider_payload(raw: Mapping[str, Any]) -> bool:
    """只有能证明是厂商原始字段（SDK 未归一）时才能标 provider_raw。"""

    return bool({"input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"} & set(raw)) and not (
        {"prompt_tokens", "completion_tokens"} & set(raw)
    )
