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
        combined_probability=0.62, yes_ask_cents=40.0, cfg=DEFAULT_CONFIG, yes_bid_cents=38.0
    )
    assert result["side"] == "yes"
    assert result["worth_trading"] is True
    assert result["kelly_fraction"] > 0.0


def test_no_side_overpriced_is_worth_trading() -> None:
    # Fair probability 0.57 vs YES ask 0.70 → YES overpriced → buy NO, real edge.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.57, yes_ask_cents=70.0, cfg=DEFAULT_CONFIG, yes_bid_cents=68.0
    )
    assert result["side"] == "no"
    assert result["entry_price_cents"] == 32.0
    assert result["worth_trading"] is True, "NO-side mispricing must be surfaced"
    assert result["kelly_fraction"] > 0.0, "NO-side Kelly must be sized, not zero"


def test_fairly_priced_market_is_not_worth_trading() -> None:
    # Fair probability essentially equals price → no edge either way.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.50, yes_ask_cents=50.0, cfg=DEFAULT_CONFIG, yes_bid_cents=48.0
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


# --- empty-data guard (#12) + same-origin source collapse (#13) ---

def test_empty_data_x_estimate_is_dropped() -> None:
    # An X estimate with metadata.data_quality == "empty" (zero posts) is
    # absence-inferred, not evidence. It must be dropped regardless of the low
    # uncertainty the agent stamped on it.
    estimates = [
        {"source": "x_grok_buzz", "probability": 0.08, "uncertainty": 0.07,
         "weight": 0.25, "metadata": {"data_quality": "empty", "post_count": 0}},
    ]
    assert score_signals.usable_estimates(estimates) == []


def test_post_count_zero_estimate_is_dropped() -> None:
    estimates = [
        {"source": "x_grok_news", "probability": 0.1, "uncertainty": 0.1,
         "weight": 0.25, "metadata": {"post_count": 0}},
    ]
    assert score_signals.usable_estimates(estimates) == []


def test_x_grok_slices_collapse_to_one_source() -> None:
    # Four x_grok strategy slices come from ONE Grok call — they must count as a
    # single source (not inflate n_sources to 4 or get 4x the weight).
    estimates = [
        {"source": "x_grok_buzz", "probability": 0.20, "uncertainty": 0.10, "weight": 0.25,
         "data_age_minutes": 0.0},
        {"source": "x_grok_sentiment", "probability": 0.30, "uncertainty": 0.10, "weight": 0.25,
         "data_age_minutes": 0.0},
        {"source": "x_grok_experts", "probability": 0.20, "uncertainty": 0.10, "weight": 0.25,
         "data_age_minutes": 0.0},
        {"source": "x_grok_news", "probability": 0.30, "uncertainty": 0.10, "weight": 0.25,
         "data_age_minutes": 0.0},
    ]
    collapsed = score_signals.collapse_source_families(estimates)
    assert len(collapsed) == 1
    assert collapsed[0]["source"] == "x_grok"
    assert collapsed[0]["probability"] == 0.25  # mean of 0.20/0.30/0.20/0.30


def test_x_grok_slices_count_as_single_source_in_score_market() -> None:
    # micro + kalshi_bias + 4 x_grok slices must yield n_sources == 3, not 6.
    market = {
        "ticker": "KX-LA", "yes_ask": 48.0, "yes_bid": 46.0,
        "signal_estimates": [
            {"source": "microstructure", "probability": 0.45, "uncertainty": 0.2, "weight": 0.4},
            {"source": "kalshi_bias", "probability": 0.50, "uncertainty": 0.05, "weight": 0.7},
            {"source": "x_grok_buzz", "probability": 0.20, "uncertainty": 0.10, "weight": 0.25},
            {"source": "x_grok_sentiment", "probability": 0.20, "uncertainty": 0.10, "weight": 0.25},
            {"source": "x_grok_experts", "probability": 0.20, "uncertainty": 0.10, "weight": 0.25},
            {"source": "x_grok_news", "probability": 0.20, "uncertainty": 0.10, "weight": 0.25},
        ],
    }
    result = score_signals.score_market(market, DEFAULT_CONFIG)
    assert result["n_sources"] == 3
    assert sorted(result["sources"]) == ["kalshi_bias", "microstructure", "x_grok"]


# --- #14 high-entry-price guardrail + configurable edge bar ---

def test_high_entry_price_leg_is_blocked() -> None:
    # NO leg entered at 93c (yes_ask=7 → NO price 93). Even with a nominal edge,
    # the >=90c payoff asymmetry (risk ~93 to make ~7) must block worth_trading.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.02, yes_ask_cents=7.0, cfg={}, yes_bid_cents=6.0)
    assert result["side"] == "no"
    assert result["entry_price_cents"] > 90
    assert result["worth_trading"] is False


def test_normal_entry_price_leg_still_tradable() -> None:
    # A 40c YES entry with strong edge is unaffected by the guardrail.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.62, yes_ask_cents=40.0, cfg={}, yes_bid_cents=38.0)
    assert result["entry_price_cents"] <= 90
    assert result["worth_trading"] is True


def test_guardrail_threshold_is_configurable() -> None:
    # Lowering the cap to 30c blocks a 40c entry that would otherwise trade.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.62, yes_ask_cents=40.0,
        cfg={"max_entry_price_cents": 30.0}, yes_bid_cents=38.0)
    assert result["worth_trading"] is False


def test_edge_bar_is_configurable() -> None:
    # Raising the bar to 15c blocks a ~12c-edge trade that clears the default 5c.
    result = score_signals.compute_edge_and_kelly(
        combined_probability=0.62, yes_ask_cents=48.0,
        cfg={"min_edge_cents": 15.0}, yes_bid_cents=46.0)
    assert result["fee_adjusted_edge"] < 15.0
    assert result["worth_trading"] is False
