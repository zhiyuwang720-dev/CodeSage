"""Cost estimation: per-model prices with per-provider cache-read ratios.

Prices are real-world values that drift — treat this table as a calibration
knob, not a constant. Unknown models cost zero (never block on cost math).
"""

from __future__ import annotations

from .types import Usage

# (input, output, cache_read_ratio) USD per 1M tokens.
# deepseek-v4-flash: official pricing (2026-04); cache hit $0.0028 = 2% of input.
# Anthropic cache reads bill at 10% of input. Peak-hour 2x pricing (2026-07)
# is NOT modeled — estimate_cost reports the off-peak baseline.
# ponytail: placeholder prices for non-verified models — verify before trusting.
PRICES_PER_MILLION: dict[str, tuple[float, float, float]] = {
    "deepseek-v4-flash": (0.14, 0.28, 0.02),
    "qwen-plus": (0.40, 1.20, 0.0),
    "glm-4": (0.10, 0.10, 0.0),
    "gpt-4o": (2.50, 10.00, 0.10),
    "claude-sonnet": (3.00, 15.00, 0.10),
    "claude-opus": (15.00, 75.00, 0.10),
}


def estimate_cost(model: str, usage: Usage | None) -> float:
    """USD cost for a usage record; cache reads at the model's ratio."""
    if usage is None:
        return 0.0
    entry = PRICES_PER_MILLION.get(model)
    if entry is None:
        return 0.0
    input_price, output_price, cache_ratio = entry
    paid_input = (
        usage.input_tokens
        + usage.cache_write_tokens
        + usage.cache_read_tokens * cache_ratio
    )
    return (paid_input * input_price + usage.output_tokens * output_price) / 1_000_000
