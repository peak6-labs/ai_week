"""Tests for the deterministic kalshi_bias signal builder (no LLM).

The LLM-driven KalshiBiasAgent was non-deterministic and frequently returned no
signal even when a documented calibration bias applied. build_bias_estimate
applies the same math directly so the signal fires reliably.
"""
from __future__ import annotations

from kalshi_trader.agents.kalshi_bias_agent import build_bias_estimate


def test_political_market_below_50_pushed_toward_no() -> None:
    est = build_bias_estimate("GOV", "governor primary", "elections", 200, 26.0)
    assert est is not None
    assert est.source == "kalshi_bias"
    assert est.probability < 0.26
    assert est.metadata["direction"] == "no"


def test_political_market_above_50_pushed_toward_yes() -> None:
    est = build_bias_estimate("CA11", "CA-11 primary", "elections", 200, 81.0)
    assert est is not None
    assert est.probability > 0.81
    assert est.metadata["direction"] == "yes"


def test_nonpolitical_longshot_pushed_down() -> None:
    est = build_bias_estimate("LONG", "some event", "economics", 200, 8.0)
    assert est is not None
    assert est.probability < 0.08  # longshot overpriced


def test_midpriced_nonpolitical_has_no_bias() -> None:
    assert build_bias_estimate("MID", "some event", "economics", 200, 50.0) is None


def test_extreme_prices_return_none() -> None:
    assert build_bias_estimate("Z", "t", "elections", 200, 0.0) is None
    assert build_bias_estimate("Z", "t", "elections", 200, 100.0) is None
