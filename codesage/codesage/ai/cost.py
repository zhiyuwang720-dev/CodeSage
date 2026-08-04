"""Cost estimation: per-model prices, cache reads at 10%.

Prices are real-world values that drift — treat this table as a calibration
knob, not a constant. Unknown models cost zero (never block on cost math).
"""

from __future__ import annotations

from .types import Usage

# (input, output) USD per 1M tokens. ponytail: placeholder prices — verify
# against each provider's pricing page before trusting totals.
PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "qwen-plus": (0.40, 1.20),
    "glm-4": (0.10, 0.10),
    "gpt-4o": (2.50, 10.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
}

CACHE_READ_DISCOUNT = 0.10  # cache reads billed at 10% of input price


def estimate_cost(model: str, usage: Usage | None) -> float:
    """USD cost for a usage record; cache reads at 10%, unknown models free."""
    if usage is None:
        return 0.0
    input_price, output_price = PRICES_PER_MILLION.get(model, (0.0, 0.0))
    paid_input = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens * CACHE_READ_DISCOUNT
    return (paid_input * input_price + usage.output_tokens * output_price) / 1_000_000
