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


def _make_orderbook(bid: int, no_price: int, ticker: str = TICKER) -> OrderBookState:
    # YES bid → _bids; NO price → _asks. best_ask() returns min(_asks) = NO price = YES ask.
    state = OrderBookState()
    state.apply_delta(ticker, "yes", bid, 10)
    state.apply_delta(ticker, "no", no_price, 10)
    return state


def _make_meta(side: str = "yes", quantity: float = 10.0, exposure: float = 5.0) -> dict:
    return {"side": side, "quantity": quantity, "market_exposure_dollars": exposure}


class TestBuildPositionDict:
    def test_yes_position_uses_bid_as_current_price(self):
        state = _make_orderbook(bid=47, no_price=53)
        result = _build_position_dict(_make_meta(side="yes"), state, TICKER)
        assert result is not None
        assert result["current_price_cents"] == 47.0

    def test_no_position_uses_100_minus_ask_as_current_price(self):
        state = _make_orderbook(bid=47, no_price=53)
        result = _build_position_dict(_make_meta(side="no"), state, TICKER)
        assert result is not None
        assert result["current_price_cents"] == 47.0  # 100 - 53

    def test_midpoint_is_average_of_bid_and_ask(self):
        state = _make_orderbook(bid=47, no_price=53)
        result = _build_position_dict(_make_meta(), state, TICKER)
        assert result["midpoint_yes_price_cents"] == 50.0

    def test_returns_none_when_no_bid(self):
        state = OrderBookState()
        state.apply_delta(TICKER, "no", 53, 10)
        assert _build_position_dict(_make_meta(), state, TICKER) is None

    def test_returns_none_when_no_ask(self):
        state = OrderBookState()
        state.apply_delta(TICKER, "yes", 47, 10)
        assert _build_position_dict(_make_meta(), state, TICKER) is None

    def test_passes_through_exposure_and_quantity(self):
        state = _make_orderbook(bid=47, no_price=53)
        result = _build_position_dict(_make_meta(exposure=5.0, quantity=10.0), state, TICKER)
        assert result["market_exposure_dollars"] == 5.0
        assert result["quantity"] == 10.0


class TestSelectYesPrice:
    def test_yes_stop_loss_uses_bid(self):
        state = _make_orderbook(bid=47, no_price=53)
        assert _select_yes_price("stop_loss", "yes", state, TICKER) == 47

    def test_yes_profit_target_uses_ask(self):
        state = _make_orderbook(bid=47, no_price=53)
        assert _select_yes_price("profit_target", "yes", state, TICKER) == 53

    def test_no_stop_loss_uses_ask(self):
        # NO aggressive sell crosses YES ask side
        state = _make_orderbook(bid=47, no_price=53)
        assert _select_yes_price("stop_loss", "no", state, TICKER) == 53

    def test_no_profit_target_uses_bid(self):
        # NO passive sell rests at 100 - YES bid
        state = _make_orderbook(bid=47, no_price=53)
        assert _select_yes_price("profit_target", "no", state, TICKER) == 47

    def test_returns_none_when_no_data(self):
        state = OrderBookState()
        assert _select_yes_price("stop_loss", "yes", state, TICKER) is None

    def test_returns_none_for_no_side_when_no_data(self):
        state = OrderBookState()
        assert _select_yes_price("stop_loss", "no", state, TICKER) is None
