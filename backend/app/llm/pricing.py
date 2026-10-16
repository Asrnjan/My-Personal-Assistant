"""Per-model prices used to estimate the cost of each call.

Prices are USD per million tokens on the Claude API. Cache writes (5-minute
TTL) cost 1.25x the input price; cache reads have their own price.
"""

from __future__ import annotations

from dataclasses import dataclass

CACHE_WRITE_MULTIPLIER = 1.25
_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01),
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_read=0.20),
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_read=0.20),
}
# Unknown models are priced like the most expensive known one, so budget
# enforcement errs on the side of stopping early rather than overspending.
FALLBACK_PRICE = max(PRICES.values(), key=lambda p: p.output)


def price_for(model: str) -> ModelPrice:
    return PRICES.get(model, FALLBACK_PRICE)


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    p = price_for(model)
    total = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_creation_input_tokens * p.input * CACHE_WRITE_MULTIPLIER
        + cache_read_input_tokens * p.cache_read
    )
    return total / _PER_MILLION
