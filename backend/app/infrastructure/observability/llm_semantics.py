from __future__ import annotations

from collections.abc import Mapping
from typing import Any

OPENINFERENCE_SEMANTIC_VERSION = "0.1.35"
OTEL_GENAI_SEMANTIC_VERSION = "1.37"

_TOKEN_ATTRIBUTES = {
    "prompt_tokens": (
        "llm.token_count.prompt",
        "gen_ai.usage.input_tokens",
    ),
    "completion_tokens": (
        "llm.token_count.completion",
        "gen_ai.usage.output_tokens",
    ),
    "total_tokens": ("llm.token_count.total",),
    "cache_read_tokens": ("llm.token_count.prompt_details.cache_read",),
    "cache_write_tokens": ("llm.token_count.prompt_details.cache_write",),
    "reasoning_tokens": ("llm.token_count.completion_details.reasoning",),
}


def llm_span_attributes(
    *,
    configured_model: str | None,
    request_model: str | None,
    response_model: str | None,
    provider: str | None,
    endpoint_id: str | None,
    protocol: str | None,
    perspective: str | None,
    purpose: str | None,
    usage: Mapping[str, Any] | None,
) -> dict[str, str | int | bool]:
    """Map normalized LLM facts to one OpenInference/OTel attribute contract."""

    attributes: dict[str, str | int | bool] = {
        "openinference.span.kind": "LLM",
        "codesage.telemetry.openinference_semantic_version": OPENINFERENCE_SEMANTIC_VERSION,
        "codesage.telemetry.otel_genai_semantic_version": OTEL_GENAI_SEMANTIC_VERSION,
    }
    identity = {
        "codesage.llm.configured_model": configured_model,
        "llm.model_name": request_model,
        "gen_ai.request.model": request_model,
        "codesage.llm.response_model": response_model,
        "gen_ai.response.model": response_model,
        "llm.provider": provider,
        "gen_ai.provider.name": provider,
        "codesage.llm.endpoint_id": endpoint_id,
        "codesage.llm.protocol": protocol,
        "codesage.perspective": perspective,
        "codesage.llm.purpose": purpose,
    }
    attributes.update({key: value for key, value in identity.items() if value is not None})

    normalized = dict(usage or {})
    sources = dict(normalized.get("field_sources") or {})
    for field, semantic_keys in _TOKEN_ATTRIBUTES.items():
        value = normalized.get(field)
        source = str(sources.get(field) or "missing")
        attributes[f"codesage.usage.{field}.source"] = source
        if value is None or source not in {"provider_raw", "sdk_normalized", "derived"}:
            continue
        for key in semantic_keys:
            attributes[key] = int(value)

    attributes["codesage.usage.present"] = bool(normalized.get("usage_present", False))
    attributes["codesage.usage.source"] = str(normalized.get("usage_source") or "unknown")
    attributes["codesage.usage.anomaly_count"] = len(list(normalized.get("anomalies") or []))
    return attributes
