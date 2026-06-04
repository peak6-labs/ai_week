"""Tests for the pure Q6 mispricing-study helpers (kalshi_trader/mentions/price_study.py).

Inline data, no network — matching the repo's test style.
"""
from __future__ import annotations

import numpy
import pytest

from kalshi_trader.mentions.price_study import (
    bootstrap_coefficient_intervals,
    brier_score,
    favorite_longshot_table,
    fit_logistic_regression,
    likelihood_ratio_test,
    logistic_log_likelihood,
    logistic_predicted_probabilities,
    select_asof_price,
    standardize_features,
)

CLOSE_EPOCH_SECONDS = 1_000_000
ASOF_MINUTES = 60  # cutoff = 1_000_000 - 3600 = 996_400


def _candle(end_period_ts, *, price_close=None, price_mean=None, volume=0):
    return {"end_period_ts": end_period_ts, "price_close": price_close,
            "price_mean": price_mean, "volume": volume}


# ---------------------------------------------------------------------------
# select_asof_price
# ---------------------------------------------------------------------------

def test_select_asof_price_prefers_latest_traded_before_cutoff():
    candles = [
        _candle(995_000, price_close=40, volume=10),   # traded, before cutoff
        _candle(996_000, price_close=55, volume=5),    # traded, later, still before cutoff
        _candle(999_000, price_close=80, volume=20),   # AFTER cutoff → must be ignored
    ]
    chosen = select_asof_price(candles, CLOSE_EPOCH_SECONDS, ASOF_MINUTES)
    assert chosen["price_source"] == "traded"
    assert chosen["market_price"] == pytest.approx(0.55)
    assert chosen["candle_end_period_ts"] == 996_000


def test_select_asof_price_falls_back_to_mid_when_no_trade():
    candles = [_candle(995_000, price_close=None, price_mean=30, volume=0)]
    chosen = select_asof_price(candles, CLOSE_EPOCH_SECONDS, ASOF_MINUTES)
    assert chosen["price_source"] == "mid_no_trade"
    assert chosen["market_price"] == pytest.approx(0.30)


def test_select_asof_price_returns_none_when_no_usable_price():
    # A candle with a close but zero volume and no mid is not a usable price.
    candles = [_candle(995_000, price_close=50, price_mean=None, volume=0)]
    assert select_asof_price(candles, CLOSE_EPOCH_SECONDS, ASOF_MINUTES) is None


def test_select_asof_price_returns_none_when_nothing_before_cutoff():
    candles = [_candle(999_000, price_close=50, volume=10)]  # only after cutoff
    assert select_asof_price(candles, CLOSE_EPOCH_SECONDS, ASOF_MINUTES) is None


# ---------------------------------------------------------------------------
# favorite-longshot bucketing
# ---------------------------------------------------------------------------

def test_favorite_longshot_bias_signs():
    # Longshots (priced 0.03) that realize often → positive bias; favorites (0.97)
    # that realize less often → negative bias.
    rows = (
        [{"market_price": 0.03, "realized_outcome": 1} for _ in range(6)]
        + [{"market_price": 0.03, "realized_outcome": 0} for _ in range(4)]
        + [{"market_price": 0.97, "realized_outcome": 0} for _ in range(6)]
        + [{"market_price": 0.97, "realized_outcome": 1} for _ in range(4)]
    )
    table = favorite_longshot_table(rows)
    longshot_bin = next(row for row in table if row["bin"] == "[0.00,0.05)")
    favorite_bin = next(row for row in table if row["bin"] == "[0.95,1.01)")
    assert longshot_bin["count"] == 10
    assert longshot_bin["signed_bias"] > 0     # realized 0.6 vs priced 0.03
    assert favorite_bin["signed_bias"] < 0      # realized 0.4 vs priced 0.97


# ---------------------------------------------------------------------------
# hand-rolled logistic regression
# ---------------------------------------------------------------------------

def test_logistic_recovers_positive_slope_on_separable_data():
    market_price = numpy.array([0.1, 0.2, 0.3, 0.4, 0.45, 0.55, 0.6, 0.7, 0.8, 0.9])
    outcomes = (market_price > 0.5).astype(float)
    standardized, _, _ = standardize_features(market_price.reshape(-1, 1))
    coefficients = fit_logistic_regression(standardized, outcomes)
    # Intercept + one slope; the price slope must be strongly positive.
    assert coefficients.shape == (2,)
    assert coefficients[1] > 1.0
    predicted = logistic_predicted_probabilities(standardized, coefficients)
    in_sample_brier = brier_score(
        [{"realized_outcome": o, "prediction": p} for o, p in zip(outcomes, predicted)],
        "prediction",
    )
    assert in_sample_brier < 0.1


def test_likelihood_ratio_test_basic():
    # A strictly better-fitting full model → p-value below 1; equal fit → exactly 1.
    assert likelihood_ratio_test(-10.0, -12.0, degrees_of_freedom=1) < 1.0
    assert likelihood_ratio_test(-10.0, -10.0, degrees_of_freedom=1) == 1.0


def test_bootstrap_intervals_have_one_row_per_coefficient():
    market_price = numpy.linspace(0.1, 0.9, 40)
    p_gdelt = numpy.linspace(0.2, 0.8, 40)
    outcomes = (market_price > 0.5).astype(float)
    features, _, _ = standardize_features(numpy.column_stack([market_price, p_gdelt]))
    intervals = bootstrap_coefficient_intervals(features, outcomes, draws=200, seed=7)
    assert len(intervals) == 3  # intercept + 2 features
    for interval in intervals:
        assert interval["percentile_2_5"] <= interval["percentile_97_5"]
