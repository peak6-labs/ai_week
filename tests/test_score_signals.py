"""Tests for the deterministic signal scorer CLI (scripts/score_signals.py).

Focus: edge and Kelly must be evaluated on the *side actually taken*, so a NO-side
mispricing (YES overpriced) is surfaced just like a YES-side one. Previously the
``side == "no"`` branch was dead — edge was only ever measured on the YES axis,
so NO opportunities were silently dropped.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

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


def test_usable_estimates_drops_empty_ensemble() -> None:
    # An ensemble estimate flagged empty (Open-Meteo down / too few members) must
    # be dropped so the parametric NOAA fallback carries the market instead.
    estimates = [
        {"source": "noaa_gfs", "probability": 0.7, "uncertainty": 0.08, "weight": 0.85},
        {"source": "gfs_ensemble", "probability": 0.5, "uncertainty": 1.0, "weight": 0.85,
         "metadata": {"data_quality": "empty", "member_count": 3}},
    ]
    usable = score_signals.usable_estimates(estimates)
    assert [u["source"] for u in usable] == ["noaa_gfs"]


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


def test_x_profile_collapses_into_x_grok_family() -> None:
    # The mentions X-profile slice (x_grok_profile) shares the X family with the
    # broad x-signal slices, so they must collapse to ONE x_grok source — the
    # profile scan must not double-count X.
    estimates = [
        {"source": "x_grok_profile", "probability": 0.70, "uncertainty": 0.20, "weight": 0.42,
         "data_age_minutes": 0.0},
        {"source": "x_grok_sentiment", "probability": 0.50, "uncertainty": 0.10, "weight": 0.25,
         "data_age_minutes": 0.0},
    ]
    collapsed = score_signals.collapse_source_families(estimates)
    assert len(collapsed) == 1
    assert collapsed[0]["source"] == "x_grok"
    assert collapsed[0]["probability"] == 0.60  # mean of 0.70/0.50


def test_mentions_base_plus_x_profile_count_as_two_sources() -> None:
    # mentions_base is its own family; x_grok_profile folds into x_grok → 2 sources.
    market = {
        "ticker": "KXMENTION-TRUMP-URANIUM", "yes_ask": 40.0, "yes_bid": 38.0,
        "signal_estimates": [
            {"source": "mentions_base", "probability": 0.30, "uncertainty": 0.18, "weight": 0.55},
            {"source": "x_grok_profile", "probability": 0.70, "uncertainty": 0.20, "weight": 0.42},
        ],
    }
    result = score_signals.score_market(market, DEFAULT_CONFIG)
    assert result["n_sources"] == 2
    assert sorted(result["sources"]) == ["mentions_base", "x_grok"]


def test_mentions_base_and_live_agreement_gets_no_boost() -> None:
    # mentions_base + mentions_live both derive from GDELT (live is independent=False),
    # so even agreeing within the tight spread they must NOT earn the agreement boost.
    cfg = {"agreement_boost_enabled": True, "agreement_spread_threshold": 0.03,
           "agreement_uncertainty_factor": 0.85}
    correlated = score_signals.combine_signals([
        {"source": "mentions_base", "probability": 0.90, "uncertainty": 0.18, "weight": 0.55,
         "data_age_minutes": 0.0, "independent": True},
        {"source": "mentions_live", "probability": 0.92, "uncertainty": 0.08, "weight": 0.85,
         "data_age_minutes": 0.0, "independent": False},
    ], cfg)
    # Weighted-average uncertainty with no boost applied.
    expected_unc = (0.55 * 0.18 + 0.85 * 0.08) / (0.55 + 0.85)
    assert correlated["uncertainty"] == pytest.approx(round(expected_unc, 4))


def test_two_independent_sources_agreement_does_get_boost() -> None:
    # Control: the same tight agreement between two independent sources IS boosted.
    cfg = {"agreement_boost_enabled": True, "agreement_spread_threshold": 0.03,
           "agreement_uncertainty_factor": 0.85}
    boosted = score_signals.combine_signals([
        {"source": "mentions_base", "probability": 0.90, "uncertainty": 0.18, "weight": 0.55,
         "data_age_minutes": 0.0, "independent": True},
        {"source": "polymarket_price", "probability": 0.92, "uncertainty": 0.08, "weight": 0.85,
         "data_age_minutes": 0.0, "independent": True},
    ], cfg)
    plain_unc = (0.55 * 0.18 + 0.85 * 0.08) / (0.55 + 0.85)
    assert boosted["uncertainty"] < round(plain_unc, 4)


def test_independent_false_propagates_through_family_collapse() -> None:
    # A non-independent slice taints the whole collapsed family.
    collapsed = score_signals.collapse_source_families([
        {"source": "x_grok_profile", "probability": 0.7, "uncertainty": 0.2, "weight": 0.42,
         "data_age_minutes": 0.0, "independent": False},
        {"source": "x_grok_sentiment", "probability": 0.7, "uncertainty": 0.1, "weight": 0.25,
         "data_age_minutes": 0.0, "independent": True},
    ])
    assert len(collapsed) == 1
    assert collapsed[0]["independent"] is False


def test_hearing_schedule_veto_pulls_combined_toward_no() -> None:
    # A near-veto schedule signal (the hearing was canceled) must drag a
    # moderately-bullish mentions_base strongly toward NO, with 2 sources.
    market = {
        "ticker": "KXMENTION-POWELL-RECESSION", "yes_ask": 60.0, "yes_bid": 58.0,
        "signal_estimates": [
            {"source": "mentions_base", "probability": 0.60, "uncertainty": 0.18, "weight": 0.55},
            {"source": "hearing_schedule", "probability": 0.03, "uncertainty": 0.05, "weight": 0.95},
        ],
    }
    result = score_signals.score_market(market, DEFAULT_CONFIG)
    assert result["n_sources"] == 2
    assert sorted(result["sources"]) == ["hearing_schedule", "mentions_base"]
    assert result["combined_probability"] < 0.30  # pulled strongly toward NO


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


# --- independence-gated agreement boost (weather NOAA + X authority) ---

def _recent_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def test_independent_agreement_reduces_combined_uncertainty() -> None:
    signals = [
        {"source": "noaa_gfs", "probability": 0.70, "uncertainty": 0.08,
         "weight": 0.85, "data_age_minutes": 0.0, "independent_of_noaa": True},
        {"source": "x_weather_authority", "probability": 0.71, "uncertainty": 0.10,
         "weight": 0.70, "data_age_minutes": 0.0, "independent_of_noaa": True},
    ]
    boosted = score_signals.combine_signals(signals, {})  # default enabled
    disabled = score_signals.combine_signals(signals, {"agreement_boost_enabled": False})
    assert boosted["n_sources"] == 2
    assert boosted["uncertainty"] < disabled["uncertainty"]


def test_nws_office_authority_agreement_gets_no_boost() -> None:
    # NWS-office authority (independent_of_noaa False) agreeing with noaa_gfs is
    # circular — the boost must NOT fire.
    signals = [
        {"source": "noaa_gfs", "probability": 0.70, "uncertainty": 0.08,
         "weight": 0.85, "data_age_minutes": 0.0, "independent_of_noaa": True},
        {"source": "x_weather_authority", "probability": 0.71, "uncertainty": 0.15,
         "weight": 0.70, "data_age_minutes": 0.0, "independent_of_noaa": False},
    ]
    enabled = score_signals.combine_signals(signals, {})
    disabled = score_signals.combine_signals(signals, {"agreement_boost_enabled": False})
    assert enabled["uncertainty"] == disabled["uncertainty"]


def test_disagreeing_pair_gets_penalty_not_boost() -> None:
    signals = [
        {"source": "noaa_gfs", "probability": 0.70, "uncertainty": 0.08,
         "weight": 0.85, "data_age_minutes": 0.0, "independent_of_noaa": True},
        {"source": "x_weather_authority", "probability": 0.50, "uncertainty": 0.10,
         "weight": 0.70, "data_age_minutes": 0.0, "independent_of_noaa": True},
    ]
    result = score_signals.combine_signals(signals, {})
    plain_weighted = (0.85 * 0.08 + 0.70 * 0.10) / (0.85 + 0.70)
    # spread 0.20 > 0.10 → disagreement penalty raises uncertainty above the
    # plain weighted average; no boost.
    assert result["uncertainty"] > plain_weighted + 0.05


def test_weather_authority_counts_as_distinct_source_no_collapse() -> None:
    now = _recent_iso()
    market = {
        "ticker": "KXHIGHTDAL-26JUN05-T85", "yes_ask": 50.0,
        "signal_estimates": [
            {"source": "noaa_gfs", "probability": 0.70, "uncertainty": 0.08,
             "weight": 0.85, "data_issued_at": now,
             "metadata": {"forecast_model": "noaa_gfs"}},
            {"source": "x_weather_authority", "probability": 0.71, "uncertainty": 0.10,
             "weight": 0.70, "data_issued_at": now,
             "metadata": {"forecast_model": "x_weather_authority",
                          "post_count": 2, "independent_of_noaa": True}},
        ],
    }
    result = score_signals.score_market(market, DEFAULT_CONFIG)
    assert result["n_sources"] == 2
    assert sorted(result["sources"]) == ["noaa_gfs", "x_weather_authority"]


def test_score_market_independent_authority_beats_nws_office_uncertainty() -> None:
    now = _recent_iso()

    def build_market(independent: bool) -> dict:
        return {
            "ticker": "KXHIGHTBOS-26JUN05-T85", "yes_ask": 50.0,
            "signal_estimates": [
                {"source": "noaa_gfs", "probability": 0.70, "uncertainty": 0.08,
                 "weight": 0.85, "data_issued_at": now, "metadata": {}},
                {"source": "x_weather_authority", "probability": 0.71, "uncertainty": 0.10,
                 "weight": 0.70, "data_issued_at": now,
                 "metadata": {"post_count": 2, "independent_of_noaa": independent}},
            ],
        }

    independent_result = score_signals.score_market(build_market(True), DEFAULT_CONFIG)
    nws_office_result = score_signals.score_market(build_market(False), DEFAULT_CONFIG)
    # Same inputs except the independence flag: the independent pair is boosted
    # (lower combined uncertainty), the NWS-office pair is not.
    assert independent_result["uncertainty"] < nws_office_result["uncertainty"]


def test_agreement_boost_respects_uncertainty_floor() -> None:
    # Two ultra-confident agreeing sources cannot drive combined uncertainty
    # below the floor even with an aggressive factor.
    signals = [
        {"source": "noaa_gfs", "probability": 0.70, "uncertainty": 0.01,
         "weight": 0.85, "data_age_minutes": 0.0, "independent_of_noaa": True},
        {"source": "x_weather_authority", "probability": 0.70, "uncertainty": 0.01,
         "weight": 0.70, "data_age_minutes": 0.0, "independent_of_noaa": True},
    ]
    result = score_signals.combine_signals(signals, {"agreement_uncertainty_factor": 0.5})
    assert result["uncertainty"] >= score_signals._AGREEMENT_UNCERTAINTY_FLOOR
