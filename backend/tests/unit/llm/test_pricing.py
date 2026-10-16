from __future__ import annotations

import pytest

from app.llm.pricing import FALLBACK_PRICE, PRICES, estimate_cost, price_for


def test_haiku_and_sonnet_costs() -> None:
    assert estimate_cost(
        "claude-haiku-5-5", input_tokens=1_000_000, output_tokens=1_000_000
    ) == pytest.approx(0.60)
    assert estimate_cost(
        "claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=1_000_000
    ) == pytest.approx(12.00)


def test_cache_tokens_are_priced() -> None:
    cost = estimate_cost(
        "claude-sonnet-5-5",
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    assert cost == pytest.approx(2.00 * 1.25 + 0.20)


def test_unknown_model_priced_conservatively() -> None:
    assert price_for("claude-unknown") == FALLBACK_PRICE
    assert max(p.output for p in PRICES.values()) == FALLBACK_PRICE.output
