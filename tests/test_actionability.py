"""Tests for kalshi_trader/actionability — signal functions, MarketScorer, SnapshotStore."""
from __future__ import annotations

import tempfile
import time
from datetime import datetime

import pytest

from kalshi_trader.models import Candle, Market, ScoredMarket
from kalshi_trader.actionability import (
    MarketScorer,
    SnapshotStore,
    hl_position_score,
    momentum_score,
    ofi_score,
    oi_change_score,
    orderbook_skew_score,
    relative_historical_volume_score,
    volume_oi_ratio_score,
    volume_spike_short_term_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(
    ticker: str = "TEST",
    volume_24h: int = 1000,
    open_interest: int = 2000,
    yes_bid: float = 40.0,
    yes_ask: float = 44.0,
) -> Market:
    return Market(
        ticker=ticker, event_ticker="EV", series_ticker="SRS",
        title="Test", yes_bid=yes_bid, yes_ask=yes_ask, last_price=42.0,
        volume_24h=volume_24h, open_interest=open_interest,
        category="sports", close_time=datetime(2026, 7, 1), status="open",
    )


def _candle(
    timestamp_seconds: int = 0,
    volume: float = 100.0,
    open_interest: float = 500.0,
    price_close: float | None = 50.0,
) -> Candle:
    return Candle(
        end_period_ts=timestamp_seconds,
        volume=volume,
        open_interest=open_interest,
        price_open=price_close,
        price_high=price_close,
        price_low=price_close,
        price_close=price_close,
        price_mean=price_close,
        price_previous=price_close,
    )


def _store() -> SnapshotStore:
    """Return a fresh in-memory SQLite SnapshotStore for tests."""
    return SnapshotStore(db_path=":memory:")


# ---------------------------------------------------------------------------
# volume_oi_ratio_score
# ---------------------------------------------------------------------------

def test_volume_oi_ratio_score_high_turnover():
    market = _market(volume_24h=2000, open_interest=2000)  # 100% turnover
    assert volume_oi_ratio_score(market) == 1.0


def test_volume_oi_ratio_score_low_turnover():
    market = _market(volume_24h=10, open_interest=2000)  # 0.5% turnover
    score = volume_oi_ratio_score(market)
    assert score == pytest.approx(0.01, abs=0.001)


def test_volume_oi_ratio_score_zero_oi():
    market = _market(volume_24h=500, open_interest=0)
    assert volume_oi_ratio_score(market) == 0.0


# ---------------------------------------------------------------------------
# relative_historical_volume_score
# ---------------------------------------------------------------------------

def test_relative_historical_volume_at_baseline():
    candles = [_candle(timestamp_seconds=i, volume=100.0) for i in range(30)]
    score = relative_historical_volume_score(candles, volume_24h=100)
    assert score == pytest.approx(0.0, abs=0.01)


def test_relative_historical_volume_3x_baseline():
    candles = [_candle(timestamp_seconds=i, volume=100.0) for i in range(30)]
    score = relative_historical_volume_score(candles, volume_24h=300)
    assert score == pytest.approx(1.0, abs=0.01)


def test_relative_historical_volume_insufficient_history():
    candles = [_candle(timestamp_seconds=i, volume=100.0) for i in range(2)]
    assert relative_historical_volume_score(candles, volume_24h=500) is None


# ---------------------------------------------------------------------------
# volume_spike_short_term_score
# ---------------------------------------------------------------------------

def test_volume_spike_flat():
    candles = [_candle(timestamp_seconds=i, volume=100.0) for i in range(24)]
    score = volume_spike_short_term_score(candles)
    assert score == pytest.approx(0.0, abs=0.01)


def test_volume_spike_2_5x():
    # Last candle is 2.5× the baseline average
    candles = [_candle(timestamp_seconds=i, volume=100.0) for i in range(23)]
    candles.append(_candle(timestamp_seconds=23, volume=250.0))
    score = volume_spike_short_term_score(candles)
    assert score == pytest.approx(1.0, abs=0.01)


def test_volume_spike_insufficient_candles():
    candles = [_candle(timestamp_seconds=i, volume=100.0) for i in range(3)]
    assert volume_spike_short_term_score(candles) is None


# ---------------------------------------------------------------------------
# oi_change_score
# ---------------------------------------------------------------------------

def test_oi_change_growing():
    candles = [_candle(timestamp_seconds=0, open_interest=1000.0), _candle(timestamp_seconds=1, open_interest=1100.0)]
    score = oi_change_score(candles)
    assert score == pytest.approx(1.0, abs=0.01)


def test_oi_change_flat():
    candles = [_candle(timestamp_seconds=0, open_interest=1000.0), _candle(timestamp_seconds=1, open_interest=1000.0)]
    assert oi_change_score(candles) == pytest.approx(0.0)


def test_oi_change_shrinking():
    candles = [_candle(timestamp_seconds=0, open_interest=1000.0), _candle(timestamp_seconds=1, open_interest=900.0)]
    assert oi_change_score(candles) == pytest.approx(0.0)


def test_oi_change_no_candles():
    assert oi_change_score([]) is None


# ---------------------------------------------------------------------------
# momentum_score
# ---------------------------------------------------------------------------

def test_momentum_10_cent_move():
    candles = [_candle(timestamp_seconds=i, price_close=40.0 + i * (10.0 / 3)) for i in range(4)]
    score = momentum_score(candles)
    assert score == pytest.approx(1.0, abs=0.01)


def test_momentum_flat():
    candles = [_candle(timestamp_seconds=i, price_close=50.0) for i in range(4)]
    assert momentum_score(candles) == pytest.approx(0.0)


def test_momentum_all_none_prices():
    candles = [_candle(timestamp_seconds=i, price_close=None) for i in range(4)]
    assert momentum_score(candles) == 0.0


def test_momentum_insufficient_candles():
    assert momentum_score([_candle()]) is None


# ---------------------------------------------------------------------------
# hl_position_score
# ---------------------------------------------------------------------------

def test_hl_at_high():
    candles = [_candle(timestamp_seconds=i, price_close=float(40 + i)) for i in range(10)]  # 40..49
    score = hl_position_score(candles, current_price=50.0)
    assert score == pytest.approx(1.0, abs=0.01)


def test_hl_at_midrange():
    candles = [_candle(timestamp_seconds=0, price_close=40.0), _candle(timestamp_seconds=1, price_close=60.0)]
    score = hl_position_score(candles, current_price=50.0)
    assert score == pytest.approx(0.0, abs=0.01)


def test_hl_flat_range():
    candles = [_candle(timestamp_seconds=i, price_close=50.0) for i in range(5)]
    assert hl_position_score(candles, current_price=50.0) is None


def test_hl_no_candles():
    assert hl_position_score([], current_price=50.0) is None


# ---------------------------------------------------------------------------
# ofi_score
# ---------------------------------------------------------------------------

def test_ofi_all_buy_yes():
    trades = [{"count_fp": "10", "taker_outcome_side": "yes"} for _ in range(5)]
    assert ofi_score(trades) == pytest.approx(1.0)


def test_ofi_balanced():
    trades = (
        [{"count_fp": "10", "taker_outcome_side": "yes"} for _ in range(5)] +
        [{"count_fp": "10", "taker_outcome_side": "no"} for _ in range(5)]
    )
    assert ofi_score(trades) == pytest.approx(0.0)


def test_ofi_empty_trades():
    assert ofi_score([]) is None


# ---------------------------------------------------------------------------
# orderbook_skew_score
# ---------------------------------------------------------------------------

def test_orderbook_skew_balanced():
    orderbook = {
        "yes": [[45, 100], [44, 50]],
        "no":  [[55, 100], [56, 50]],
    }
    score = orderbook_skew_score(orderbook)
    assert score == pytest.approx(0.0, abs=0.01)


def test_orderbook_skew_all_bids():
    orderbook = {"yes": [[45, 200]], "no": []}
    score = orderbook_skew_score(orderbook)
    assert score == pytest.approx(1.0, abs=0.01)


def test_orderbook_skew_empty():
    assert orderbook_skew_score({}) == 0.5
    assert orderbook_skew_score({"yes": [], "no": []}) == 0.5


# ---------------------------------------------------------------------------
# MarketScorer integration
# ---------------------------------------------------------------------------

def test_score_all_sorted_descending():
    markets = [
        _market("A", volume_24h=5000, open_interest=1000),  # high turnover
        _market("B", volume_24h=10,   open_interest=10000), # low turnover
        _market("C", volume_24h=2000, open_interest=2000),  # medium
    ]
    store = _store()
    scorer = MarketScorer()
    result = scorer.score_all(markets, store)
    scores = [scored_market.composite_score for scored_market in result]
    assert scores == sorted(scores, reverse=True)


def test_score_all_composites_in_range():
    markets = [_market(str(i), volume_24h=i * 100, open_interest=1000) for i in range(1, 6)]
    store = _store()
    scorer = MarketScorer()
    for scored_market in scorer.score_all(markets, store):
        assert 0.0 <= scored_market.composite_score <= 1.0


def test_score_all_empty_input():
    scorer = MarketScorer()
    store = _store()
    assert scorer.score_all([], store) == []


def test_score_all_no_candle_history_uses_volume_oi_only():
    market = _market("X", volume_24h=500, open_interest=500)
    store = _store()
    scorer = MarketScorer()
    result = scorer.score_all([market], store)
    scored_market = result[0]
    # All candle-based signals should be None (no history in store)
    assert scored_market.relative_historical_volume_score is None
    assert scored_market.volume_spike_short_term_score is None
    assert scored_market.oi_change_score is None
    # Composite must still be valid (re-normalized over volume_oi_ratio only)
    assert 0.0 <= scored_market.composite_score <= 1.0


def test_enrich_with_live_high_ofi_boosts_ranking():
    markets = [
        _market("HIGH", volume_24h=1000, open_interest=1000),
        _market("LOW",  volume_24h=1000, open_interest=1000),
    ]
    store = _store()
    # Seed candle history so signal coverage exceeds MIN_COVERAGE (30%).
    # Flat candles at 100 volume give relative_historical + volume_spike + oi_change + momentum.
    daily_candles = [_candle(timestamp_seconds=i, volume=100.0, open_interest=500.0) for i in range(30)]
    hourly_candles = [_candle(timestamp_seconds=i, volume=100.0, open_interest=500.0) for i in range(24)]
    for ticker in ("HIGH", "LOW"):
        store.update_daily(ticker, daily_candles)
        store.update_hourly(ticker, hourly_candles)

    scorer = MarketScorer()
    scored = scorer.score_all(markets, store)

    # Give HIGH all buy-YES trades, LOW balanced trades
    trade_data = {
        "HIGH": [{"count_fp": "100", "taker_outcome_side": "yes"} for _ in range(10)],
        "LOW":  (
            [{"count_fp": "50", "taker_outcome_side": "yes"} for _ in range(5)] +
            [{"count_fp": "50", "taker_outcome_side": "no"}  for _ in range(5)]
        ),
    }
    result = scorer.enrich_with_live(scored, trade_data, {})
    high = next(scored_market for scored_market in result if scored_market.market.ticker == "HIGH")
    low  = next(scored_market for scored_market in result if scored_market.market.ticker == "LOW")
    assert high.composite_score > low.composite_score


# ---------------------------------------------------------------------------
# SnapshotStore
# ---------------------------------------------------------------------------

def test_snapshot_store_is_daily_stale_initially():
    store = _store()
    assert store.is_daily_stale("UNKNOWN-TICKER") is True


def test_snapshot_store_update_daily_clears_staleness():
    store = _store()
    candles = [_candle(timestamp_seconds=i) for i in range(5)]
    store.update_daily("ABC", candles)
    assert store.is_daily_stale("ABC") is False


def test_snapshot_store_update_daily_persists():
    store = _store()
    candles = [_candle(timestamp_seconds=i, volume=float(i * 10)) for i in range(5)]
    store.update_daily("ABC", candles)
    retrieved = store.get_daily("ABC")
    assert len(retrieved) == 5
    assert retrieved[0].volume == 0.0
    assert retrieved[4].volume == 40.0


def test_snapshot_store_get_daily_unknown_ticker_returns_empty():
    store = _store()
    assert store.get_daily("NOPE") == []


def test_snapshot_store_refresh_log_respects_ttl(monkeypatch):
    store = _store()
    candles = [_candle(timestamp_seconds=i) for i in range(3)]
    store.update_daily("TTL", candles)
    assert store.is_daily_stale("TTL") is False

    # Advance time past TTL
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + store.DAILY_TTL_SECONDS + 1)
    assert store.is_daily_stale("TTL") is True


# ---------------------------------------------------------------------------
# Client method formatting
# ---------------------------------------------------------------------------

def test_get_market_candlesticks_batch_formats_tickers(monkeypatch):
    """Verify that tickers are joined as a comma-separated string in params."""
    import asyncio
    from kalshi_trader.client import KalshiClient

    captured: dict = {}

    async def fake_get(endpoint, params=None):
        captured["endpoint"] = endpoint
        captured["params"] = params
        return {"candles": []}

    client = object.__new__(KalshiClient)
    client.get = fake_get  # type: ignore[attr-defined]

    asyncio.run(client.get_market_candlesticks_batch(
        ["A", "B", "C"], start_ts=1000, end_ts=2000, period_interval=60
    ))

    assert captured["params"]["market_tickers"] == "A,B,C"
    assert captured["params"]["period_interval"] == 60


def test_get_trades_passes_min_ts(monkeypatch):
    import asyncio
    from kalshi_trader.client import KalshiClient

    captured: dict = {}

    async def fake_get(endpoint, params=None):
        captured["params"] = params
        return {"trades": []}

    client = object.__new__(KalshiClient)
    client.get = fake_get  # type: ignore[attr-defined]

    asyncio.run(client.get_trades("XYZ", min_ts=9999))
    assert captured["params"]["ticker"] == "XYZ"
    assert captured["params"]["min_ts"] == 9999
