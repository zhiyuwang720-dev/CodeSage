"""Cost estimation tests."""

import pytest

from codesage.ai import Usage, estimate_cost


def test_known_model():
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    assert estimate_cost("deepseek-v4-flash", usage) == pytest.approx(0.14)


def test_cache_reads_billed_at_model_ratio():
    # deepseek-v4-flash cache hit: $0.0028 per 1M (2% of input price)
    usage = Usage(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)
    assert estimate_cost("deepseek-v4-flash", usage) == pytest.approx(0.0028)


def test_unknown_model_free():
    usage = Usage(input_tokens=10_000_000, output_tokens=10_000_000)
    assert estimate_cost("some-future-model", usage) == 0.0


def test_none_usage_free():
    assert estimate_cost("deepseek-v4-flash", None) == 0.0


def test_output_tokens_counted():
    usage = Usage(input_tokens=0, output_tokens=1_000_000)
    assert estimate_cost("deepseek-v4-flash", usage) == pytest.approx(0.28)


def test_cache_write_priced_at_input_rate():
    usage = Usage(input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000)
    assert estimate_cost("deepseek-v4-flash", usage) == pytest.approx(0.14)
