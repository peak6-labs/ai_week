# tests/test_exit_monitor.py
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_trader.orderbook import OrderBookState

spec = importlib.util.spec_from_file_location(
    "exit_monitor", os.path.join(os.path.dirname(__file__), "..", "scripts", "exit_monitor.py")
)
exit_monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exit_monitor)

_build_position_dict = exit_monitor._build_position_dict
_select_yes_price = exit_monitor._select_yes_price

TICKER = "KXTEST-1"


def _make_orderbook(bid: int, no_bid: int, ticker: str = TICKER) -> OrderBookState:
    """Build an orderbook with a realistic spread.

    bid    = YES bid (max YES book)
    no_bid = top NO bid (max NO book) — the market NO price, NOT min
    Also adds a floor NO entry at 1 to simulate a real multi-level NO book
    where min(_asks) != max(_asks).
    """
    state = OrderBookState()
    state.apply_delta(ticker, "yes", bid, 10)
    state.apply_delta(ticker, "no", 1, 5)      # floor NO bid (always present in real book)
    state.apply_delta(ticker, "no", no_bid, 10)  # top NO bid = market level
    return state


def _make_meta(side: str = "yes", quantity: float = 10.0, exposure: float = 5.0) -> dict:
    return {"side": side, "quantity": quantity, "market_exposure_dollars": exposure}


class TestBuildPositionDict:
    # bid=47 (YES bid), no_bid=50 (top NO bid) → YES ask = 100-50 = 50, spread = 3¢
    def test_yes_position_uses_yes_bid_as_current_price(self):
        state = _make_orderbook(bid=47, no_bid=50)
        result = _build_position_dict(_make_meta(side="yes"), state, TICKER)
        assert result is not None
        assert result["current_price_cents"] == 47.0

    def test_no_position_uses_top_no_bid_as_current_price(self):
        # current NO price = max(NO book) = 50¢ (NOT 100 - min(NO book))
        state = _make_orderbook(bid=47, no_bid=50)
        result = _build_position_dict(_make_meta(side="no"), state, TICKER)
        assert result is not None
        assert result["current_price_cents"] == 50.0

    def test_no_position_uses_max_no_bid_not_min(self):
        # Confirms best_no_bid() = max(_asks), not min(_asks).
        # With floor NO bid at 1 and top at 50, current_price_cents must be 50, not 99 (=100-1).
        state = _make_orderbook(bid=47, no_bid=50)
        result = _build_position_dict(_make_meta(side="no"), state, TICKER)
        assert result["current_price_cents"] == 50.0
        assert result["current_price_cents"] != 99.0  # 100 - min = 99 was the old bug

    def test_midpoint_yes_price(self):
        # midpoint = (YES bid + YES ask) / 2 = (47 + (100 - 50)) / 2 = (47 + 50) / 2 = 48.5
        state = _make_orderbook(bid=47, no_bid=50)
        result = _build_position_dict(_make_meta(), state, TICKER)
        assert result["midpoint_yes_price_cents"] == 48.5

    def test_returns_none_when_no_yes_bid(self):
        state = OrderBookState()
        state.apply_delta(TICKER, "no", 50, 10)
        assert _build_position_dict(_make_meta(), state, TICKER) is None

    def test_returns_none_when_no_no_bid(self):
        state = OrderBookState()
        state.apply_delta(TICKER, "yes", 47, 10)
        assert _build_position_dict(_make_meta(), state, TICKER) is None

    def test_passes_through_exposure_and_quantity(self):
        state = _make_orderbook(bid=47, no_bid=50)
        result = _build_position_dict(_make_meta(exposure=5.0, quantity=10.0), state, TICKER)
        assert result["market_exposure_dollars"] == 5.0
        assert result["quantity"] == 10.0

    def test_yes_fair_value_passed_through_unchanged(self):
        meta = {**_make_meta(side="yes"), "fair_value_cents": 70.0}
        state = _make_orderbook(bid=47, no_bid=50)
        result = _build_position_dict(meta, state, TICKER)
        assert result["fair_value_cents"] == 70.0

    def test_no_fair_value_converted_to_no_side(self):
        # predicted_prob=0.70 → YES fair=70 → NO fair = 100 - 70 = 30
        meta = {**_make_meta(side="no"), "fair_value_cents": 70.0}
        state = _make_orderbook(bid=47, no_bid=50)
        result = _build_position_dict(meta, state, TICKER)
        assert result["fair_value_cents"] == 30.0

    def test_no_fair_value_absent_when_not_set(self):
        state = _make_orderbook(bid=47, no_bid=50)
        result = _build_position_dict(_make_meta(side="no"), state, TICKER)
        assert "fair_value_cents" not in result


class TestSelectYesPrice:
    # bid=47 (YES bid), no_bid=50 → YES ask = 100-50 = 50
    def test_yes_stop_loss_uses_yes_bid(self):
        state = _make_orderbook(bid=47, no_bid=50)
        assert _select_yes_price("stop_loss", "yes", state, TICKER) == 47

    def test_yes_profit_target_uses_yes_ask(self):
        # passive YES sell: yes_price = YES ask = 100 - no_bid = 50
        state = _make_orderbook(bid=47, no_bid=50)
        assert _select_yes_price("profit_target", "yes", state, TICKER) == 50

    def test_no_stop_loss_crosses_no_bid(self):
        # aggressive NO sell at NO bid: yes_price = 100 - no_bid = 50
        state = _make_orderbook(bid=47, no_bid=50)
        assert _select_yes_price("stop_loss", "no", state, TICKER) == 50

    def test_no_profit_target_rests_above_no_bid(self):
        # passive NO sell above NO bid: yes_price = yes_bid = 47
        # NO price = 100 - 47 = 53 > current NO bid of 50 → rests on book
        state = _make_orderbook(bid=47, no_bid=50)
        assert _select_yes_price("profit_target", "no", state, TICKER) == 47

    def test_returns_none_when_no_data(self):
        state = OrderBookState()
        assert _select_yes_price("stop_loss", "yes", state, TICKER) is None

    def test_returns_none_for_no_side_when_no_data(self):
        state = OrderBookState()
        assert _select_yes_price("stop_loss", "no", state, TICKER) is None


import asyncio


class TestFetchOpenPositions:
    def test_extracts_yes_position(self):
        raw = {
            "market_positions": [
                {
                    "ticker": "KXTEST-1",
                    "position_fp": "10",
                    "market_exposure_dollars": "5.00",
                }
            ]
        }

        class FakeClient:
            async def get_positions(self):
                return raw

        result = asyncio.run(exit_monitor._fetch_open_positions(FakeClient()))
        assert "KXTEST-1" in result
        pos = result["KXTEST-1"]
        assert pos["side"] == "yes"
        assert pos["quantity"] == 10.0
        assert pos["market_exposure_dollars"] == 5.0

    def test_extracts_no_position(self):
        raw = {
            "market_positions": [
                {
                    "ticker": "KXTEST-2",
                    "position_fp": "-5",
                    "market_exposure_dollars": "2.50",
                }
            ]
        }

        class FakeClient:
            async def get_positions(self):
                return raw

        result = asyncio.run(exit_monitor._fetch_open_positions(FakeClient()))
        assert result["KXTEST-2"]["side"] == "no"
        assert result["KXTEST-2"]["quantity"] == 5.0

    def test_skips_zero_position(self):
        raw = {
            "market_positions": [
                {"ticker": "KXTEST-3", "position_fp": "0", "market_exposure_dollars": "0"}
            ]
        }

        class FakeClient:
            async def get_positions(self):
                return raw

        result = asyncio.run(exit_monitor._fetch_open_positions(FakeClient()))
        assert "KXTEST-3" not in result


class TestFetchRestingSellTickers:
    def test_returns_sell_tickers(self):
        raw = {
            "orders": [
                {"ticker": "KXTEST-1", "action": "sell"},
                {"ticker": "KXTEST-2", "action": "buy"},
            ]
        }

        class FakeClient:
            async def get_orders(self, status="resting"):
                return raw

        result = asyncio.run(exit_monitor._fetch_resting_sell_tickers(FakeClient()))
        assert "KXTEST-1" in result
        assert "KXTEST-2" not in result

    def test_returns_empty_set_when_no_orders(self):
        class FakeClient:
            async def get_orders(self, status="resting"):
                return {"orders": []}

        result = asyncio.run(exit_monitor._fetch_resting_sell_tickers(FakeClient()))
        assert result == set()
