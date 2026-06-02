"""Unit tests for OrderBookState."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kalshi_trader.orderbook import OrderBookState


TICKER = "INXY-25DEC31-T49999.99"


# ---------------------------------------------------------------------------
# apply_delta
# ---------------------------------------------------------------------------

def test_apply_delta_add_bid_level():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 55, 10)
    assert state._bids[TICKER][55] == 10


def test_apply_delta_add_ask_level():
    state = OrderBookState()
    state.apply_delta(TICKER, "no", 60, 5)
    assert state._asks[TICKER][60] == 5


def test_apply_delta_update_level():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 55, 10)
    state.apply_delta(TICKER, "yes", 55, 20)
    assert state._bids[TICKER][55] == 20


def test_apply_delta_remove_level_with_zero():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 55, 10)
    state.apply_delta(TICKER, "yes", 55, 0)
    assert 55 not in state._bids[TICKER]


def test_apply_delta_remove_nonexistent_level_noop():
    state = OrderBookState()
    # Should not raise even if the price level was never set
    state.apply_delta(TICKER, "yes", 99, 0)
    assert 99 not in state._bids[TICKER]


def test_apply_delta_multiple_levels():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 50, 5)
    state.apply_delta(TICKER, "yes", 55, 10)
    state.apply_delta(TICKER, "no", 60, 8)
    assert len(state._bids[TICKER]) == 2
    assert len(state._asks[TICKER]) == 1


# ---------------------------------------------------------------------------
# apply_snapshot
# ---------------------------------------------------------------------------

def test_apply_snapshot_sets_bids_and_asks():
    state = OrderBookState()
    yes_book = [{"price": "50", "quantity": "5"}, {"price": "55", "quantity": "10"}]
    no_book = [{"price": "60", "quantity": "8"}]
    state.apply_snapshot(TICKER, yes_book, no_book)
    assert state._bids[TICKER] == {50: 5, 55: 10}
    assert state._asks[TICKER] == {60: 8}


def test_apply_snapshot_replaces_existing_state():
    state = OrderBookState()
    # Seed with old data via delta
    state.apply_delta(TICKER, "yes", 40, 100)
    state.apply_delta(TICKER, "no", 70, 200)

    yes_book = [{"price": "52", "quantity": "3"}]
    no_book = [{"price": "58", "quantity": "7"}]
    state.apply_snapshot(TICKER, yes_book, no_book)

    # Old levels gone
    assert 40 not in state._bids[TICKER]
    assert 70 not in state._asks[TICKER]
    # New levels present
    assert state._bids[TICKER] == {52: 3}
    assert state._asks[TICKER] == {58: 7}


def test_apply_snapshot_empty_books():
    state = OrderBookState()
    state.apply_snapshot(TICKER, [], [])
    assert state._bids[TICKER] == {}
    assert state._asks[TICKER] == {}


def test_apply_snapshot_does_not_affect_other_tickers():
    state = OrderBookState()
    other = "OTHER-TICKER"
    state.apply_delta(other, "yes", 50, 10)
    state.apply_snapshot(TICKER, [], [])
    assert state._bids[other][50] == 10


# ---------------------------------------------------------------------------
# bid_ask_imbalance
# ---------------------------------------------------------------------------

def test_imbalance_empty_book():
    state = OrderBookState()
    assert state.bid_ask_imbalance(TICKER) == 0.0


def test_imbalance_all_bid():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 50, 100)
    assert state.bid_ask_imbalance(TICKER) == pytest.approx(1.0)


def test_imbalance_all_ask():
    state = OrderBookState()
    state.apply_delta(TICKER, "no", 60, 100)
    assert state.bid_ask_imbalance(TICKER) == pytest.approx(-1.0)


def test_imbalance_balanced():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 50, 100)
    state.apply_delta(TICKER, "no", 60, 100)
    assert state.bid_ask_imbalance(TICKER) == pytest.approx(0.0)


def test_imbalance_bid_heavy():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 50, 75)
    state.apply_delta(TICKER, "no", 60, 25)
    # (75-25)/(75+25) = 50/100 = 0.5
    assert state.bid_ask_imbalance(TICKER) == pytest.approx(0.5)


def test_imbalance_ask_heavy():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 50, 25)
    state.apply_delta(TICKER, "no", 60, 75)
    # (25-75)/(25+75) = -50/100 = -0.5
    assert state.bid_ask_imbalance(TICKER) == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# best_bid / best_ask / spread_cents
# ---------------------------------------------------------------------------

def test_best_bid_single_level():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 55, 10)
    assert state.best_bid(TICKER) == 55


def test_best_bid_multiple_levels():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 50, 5)
    state.apply_delta(TICKER, "yes", 55, 10)
    state.apply_delta(TICKER, "yes", 45, 3)
    assert state.best_bid(TICKER) == 55


def test_best_bid_empty_returns_none():
    state = OrderBookState()
    assert state.best_bid(TICKER) is None


def test_best_ask_single_level():
    state = OrderBookState()
    state.apply_delta(TICKER, "no", 60, 5)
    assert state.best_ask(TICKER) == 60


def test_best_ask_multiple_levels():
    state = OrderBookState()
    state.apply_delta(TICKER, "no", 65, 5)
    state.apply_delta(TICKER, "no", 60, 10)
    state.apply_delta(TICKER, "no", 70, 3)
    assert state.best_ask(TICKER) == 60


def test_best_ask_empty_returns_none():
    state = OrderBookState()
    assert state.best_ask(TICKER) is None


def test_spread_cents_normal():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 55, 10)
    state.apply_delta(TICKER, "no", 60, 5)
    assert state.spread_cents(TICKER) == 5


def test_spread_cents_missing_bid():
    state = OrderBookState()
    state.apply_delta(TICKER, "no", 60, 5)
    assert state.spread_cents(TICKER) is None


def test_spread_cents_missing_ask():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 55, 10)
    assert state.spread_cents(TICKER) is None


def test_spread_cents_both_missing():
    state = OrderBookState()
    assert state.spread_cents(TICKER) is None


# ---------------------------------------------------------------------------
# volume_velocity
# ---------------------------------------------------------------------------

def _fake_now():
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_volume_velocity_empty():
    state = OrderBookState()
    assert state.volume_velocity(TICKER) == 0.0


def test_volume_velocity_all_within_window():
    state = OrderBookState()
    now = _fake_now()
    # Two trades 60 seconds ago (within 300s window)
    ts_recent = now - timedelta(seconds=60)
    state._trades[TICKER].append((ts_recent, 10))
    state._trades[TICKER].append((ts_recent, 20))

    with patch("kalshi_trader.orderbook.datetime") as mock_dt:
        mock_dt.now.return_value = now
        # window_seconds=300 → 300/60 = 5 minutes
        velocity = state.volume_velocity(TICKER, window_seconds=300)

    # total=30 contracts / 5 minutes = 6 contracts/min
    assert velocity == pytest.approx(6.0)


def test_volume_velocity_all_outside_window():
    state = OrderBookState()
    now = _fake_now()
    # Trade 600 seconds ago — outside 300s window
    ts_old = now - timedelta(seconds=600)
    state._trades[TICKER].append((ts_old, 50))

    with patch("kalshi_trader.orderbook.datetime") as mock_dt:
        mock_dt.now.return_value = now
        velocity = state.volume_velocity(TICKER, window_seconds=300)

    assert velocity == 0.0


def test_volume_velocity_mixed_window():
    state = OrderBookState()
    now = _fake_now()
    ts_recent = now - timedelta(seconds=60)
    ts_old = now - timedelta(seconds=400)
    state._trades[TICKER].append((ts_old, 100))   # outside window
    state._trades[TICKER].append((ts_recent, 30)) # inside window

    with patch("kalshi_trader.orderbook.datetime") as mock_dt:
        mock_dt.now.return_value = now
        velocity = state.volume_velocity(TICKER, window_seconds=300)

    # Only recent trade counted: 30 / 5 = 6.0
    assert velocity == pytest.approx(6.0)


def test_volume_velocity_custom_window():
    state = OrderBookState()
    now = _fake_now()
    ts_recent = now - timedelta(seconds=30)
    state._trades[TICKER].append((ts_recent, 60))

    with patch("kalshi_trader.orderbook.datetime") as mock_dt:
        mock_dt.now.return_value = now
        # window=60s → 60/60 = 1 minute
        velocity = state.volume_velocity(TICKER, window_seconds=60)

    assert velocity == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# tickers
# ---------------------------------------------------------------------------

def test_tickers_empty():
    state = OrderBookState()
    assert state.tickers() == []


def test_tickers_from_bids_and_asks():
    state = OrderBookState()
    state.apply_delta("TICK-A", "yes", 50, 5)
    state.apply_delta("TICK-B", "no", 60, 3)
    tickers = state.tickers()
    assert set(tickers) == {"TICK-A", "TICK-B"}


def test_tickers_deduped():
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 50, 5)
    state.apply_delta(TICKER, "no", 60, 3)
    assert state.tickers().count(TICKER) == 1
