"""R01（原 A03—A07）：usage 归一化与身份契约（P07）。

来源层使用 `provider_raw|sdk_normalized`：只有能证明是厂商原始字段时才标 provider_raw，
其余按 SDK 已归一处理，禁止二次归一导致缓存 token 重复相加。
"""

from __future__ import annotations

import pytest

from app.execution_plane.models.usage import normalize_usage


def test_usage_preserves_deepseek_provider_occurrences_without_double_counting() -> None:
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
    assert usage.field_sources["prompt_tokens"] == "sdk_normalized"
    assert usage.usage_source == "sdk_normalized"
    assert usage.normalization_version == "2"
    assert usage.anomalies == []


def test_missing_usage_is_not_explicit_zero() -> None:
    assert normalize_usage(None, provider="deepseek") is None
    assert normalize_usage({}, provider="deepseek") is None

    usage = normalize_usage(
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        provider="deepseek",
        zero_fidelity="provider",
    )
    assert usage is not None
    assert usage.prompt_tokens == 0
    assert usage.total_tokens == 0
    assert usage.field_sources["total_tokens"] == "sdk_normalized"
    assert usage.usage_present is True


def test_sdk_synthetic_zero_is_missing_with_anomaly() -> None:
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
    ],
)
def test_invalid_or_conflicting_usage_is_not_silently_coerced(
    raw: dict[str, object], expected_anomaly: str
) -> None:
    usage = normalize_usage(raw, provider="deepseek")
    assert usage is not None
    assert expected_anomaly in usage.anomalies


def test_openai_cached_and_reasoning_details_are_preserved() -> None:
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


def test_anthropic_raw_categories_keep_input_without_second_normalization() -> None:
    """SDK 已归一字段只做一次映射；不再自行叠加 input+cache 得到新的 prompt。"""

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
    assert usage.prompt_tokens == 600
    assert usage.cache_read_tokens == 300
    assert usage.cache_write_tokens == 100
    assert usage.usage_source == "provider_raw"
    assert usage.field_sources["prompt_tokens"] == "provider_raw"


def test_unknown_source_stays_unknown_without_provider_guessing() -> None:
    usage = normalize_usage(
        {"total_tokens": 5},
        provider="deepseek",
    )
    assert usage is not None
    assert usage.usage_source == "sdk_normalized"
    assert usage.prompt_tokens is None
