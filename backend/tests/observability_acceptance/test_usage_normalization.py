from __future__ import annotations

import pytest

from app.execution_plane.models.usage import normalize_usage


def test_a03_deepseek_cache_usage_is_not_double_counted() -> None:
    usage = normalize_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_cache_hit_tokens": 400,
            "prompt_cache_miss_tokens": 600,
        },
        provider="deepseek",
        protocol="openai_chat",
    )

    assert usage is not None
    assert usage.prompt_tokens == 1000
    assert usage.cache_read_tokens == 400
    assert usage.total_tokens == 1200
    assert usage.field_sources["cache_read_tokens"] == "provider"
    assert usage.anomalies == []


def test_a04_missing_usage_is_not_explicit_zero() -> None:
    assert normalize_usage(None, provider="deepseek") is None

    usage = normalize_usage(
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        provider="deepseek",
    )
    assert usage is not None
    assert usage.prompt_tokens == 0
    assert usage.total_tokens == 0
    assert usage.field_sources["total_tokens"] == "provider"


def test_a04_unverified_sdk_zero_is_missing_with_anomaly() -> None:
    usage = normalize_usage(
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        provider="deepseek",
        zero_fidelity="unverified",
        estimated_input_tokens=11,
        estimated_output_tokens=7,
    )
    assert usage is not None
    assert usage.prompt_tokens is None
    assert usage.total_tokens is None
    assert usage.estimated_usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "method": "tiktoken",
        "version": "1",
    }
    assert "usage:synthetic_zero_unverified" in usage.anomalies


@pytest.mark.parametrize(
    ("raw", "expected_anomaly"),
    [
        ({"prompt_tokens": -1}, "prompt_tokens:negative"),
        ({"prompt_tokens": 1.5}, "prompt_tokens:invalid_integer"),
        (
            {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 99},
            "total_tokens:sum_mismatch",
        ),
        (
            {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "prompt_cache_hit_tokens": 8,
                "prompt_cache_miss_tokens": 8,
            },
            "prompt_tokens:cache_parts_mismatch",
        ),
    ],
)
def test_a06_invalid_or_conflicting_usage_is_not_silently_coerced(
    raw: dict[str, object], expected_anomaly: str
) -> None:
    usage = normalize_usage(raw, provider="deepseek")
    assert usage is not None
    assert expected_anomaly in usage.anomalies


def test_a07_openai_cached_and_reasoning_details_are_preserved() -> None:
    usage = normalize_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 400},
            "completion_tokens_details": {"reasoning_tokens": 50},
        },
        provider="openai",
        protocol="openai_chat",
    )
    assert usage is not None
    assert usage.cache_read_tokens == 400
    assert usage.reasoning_tokens == 50
    assert usage.total_tokens == 1200


def test_a07_anthropic_input_includes_non_overlapping_cache_categories() -> None:
    usage = normalize_usage(
        {
            "input_tokens": 600,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 100,
            "output_tokens": 200,
        },
        provider="claude",
        protocol="anthropic_messages",
    )
    assert usage is not None
    assert usage.prompt_tokens == 1000
    assert usage.cache_read_tokens == 300
    assert usage.cache_write_tokens == 100
    assert usage.total_tokens == 1200
    assert usage.field_sources["prompt_tokens"] == "derived"
