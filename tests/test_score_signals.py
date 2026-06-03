"""Tests for the deterministic signal scorer CLI (scripts/score_signals.py).

Focus: edge and Kelly must be evaluated on the *side actually taken*, so a NO-side
mispricing (YES overpriced) is surfaced just like a YES-side one. Previously the
``side == "no"`` branch was dead — edge was only ever measured on the YES axis,
so NO opportunities were silently dropped.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "score_signals.py"
_spec = importlib.util.spec_from_file_location("score_signals", _MODULE_PATH)
score_signals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score_signals)

DEFAULT_CONFIG: dict = {}


def test_yes_side_underpriced_is_worth_trading() -> None:
    # Fair probability 0.62 vs YES ask 0.40 → buy YES, large positive edge.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.62, yes_ask_cents=40.0, cfg=DEFAULT_CONFIG
    )
    assert result["side"] == "yes"
    assert result["worth_trading"] is True
    assert result["kelly_fraction"] > 0.0


def test_no_side_overpriced_is_worth_trading() -> None:
    # Fair probability 0.57 vs YES ask 0.70 → YES overpriced → buy NO, real edge.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.57, yes_ask_cents=70.0, cfg=DEFAULT_CONFIG
    )
    assert result["side"] == "no"
    assert result["worth_trading"] is True, "NO-side mispricing must be surfaced"
    assert result["kelly_fraction"] > 0.0, "NO-side Kelly must be sized, not zero"


def test_fairly_priced_market_is_not_worth_trading() -> None:
    # Fair probability essentially equals price → no edge either way.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.50, yes_ask_cents=50.0, cfg=DEFAULT_CONFIG
    )
    assert result["worth_trading"] is False
    assert result["kelly_fraction"] == 0.0


# --- direct-combine path: agent SignalEstimate arrays -----------------------

def test_usable_estimates_drops_noninformative() -> None:
    estimates = [
        {"source": "kalshi_bias", "probability": 0.62, "uncertainty": 0.05, "weight": 0.65},
        {"source": "x_grok_buzz", "probability": 0.5, "uncertainty": 1.0, "weight": 0.6},  # null
    ]
    usable = score_signals.usable_estimates(estimates)
    assert [u["source"] for u in usable] == ["kalshi_bias"]


def test_score_market_uses_signal_estimates_directly() -> None:
    market = {
        "ticker": "KXFOO", "title": "t", "category": "politics", "yes_ask": 40.0,
        "hours_to_close": 24.0,
        "signal_estimates": [
            {"source": "kalshi_bias", "probability": 0.6, "uncertainty": 0.05, "weight": 0.65},
            {"source": "polymarket_price", "probability": 0.58, "uncertainty": 0.03, "weight": 0.75},
        ],
    }
    result = score_signals.score_market(market, DEFAULT_CONFIG)
    assert result["n_sources"] == 2
    assert result["side"] == "yes"  # fair ~0.59 > price 0.40
    assert result["worth_trading"] is True


def test_score_market_all_null_estimates_not_worth_trading() -> None:
    market = {
        "ticker": "KXBAR", "yes_ask": 56.0,
        "signal_estimates": [
            {"source": "x_grok_buzz", "probability": 0.5, "uncertainty": 1.0, "weight": 0.6},
            {"source": "x_grok_news", "probability": 0.5, "uncertainty": 1.0, "weight": 0.6},
        ],
    }
    result = score_signals.score_market(market, DEFAULT_CONFIG)
    assert result["n_sources"] == 0
    assert result["worth_trading"] is False


def test_no_signals_against_extreme_price_is_not_actionable() -> None:
    # 0 sources → combined defaults to 0.5; against a 3c longshot this must NOT
    # fabricate a huge edge. n_sources==0 forces worth_trading off.
    market = {"ticker": "FEDHIKE", "yes_ask": 3.0, "signal_estimates": []}
    result = score_signals.score_market(market, DEFAULT_CONFIG)
    assert result["n_sources"] == 0
    assert result["worth_trading"] is False
    assert result["edge_cents"] == 0.0
    assert result["kelly_fraction"] == 0.0
