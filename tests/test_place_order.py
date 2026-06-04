"""Tests for scripts/place_order.py"""
from __future__ import annotations
import importlib
import math
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import kalshi_trader.config  # noqa: F401

place_order = importlib.import_module("scripts.place_order")


def _ob(yes_bid: int, no_bid: int) -> dict:
    """Build a minimal normalized orderbook dict."""
    return {"orderbook": {"yes": [[yes_bid, 100]], "no": [[no_bid, 100]]}}


# --- midmarket_maker ---

def test_midmarket_maker_sell_rounds_up():
    # best_bid=62, best_ask=65 (100-35), midpoint=63.5, ceil=64
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "midmarket_maker") == 64


def test_midmarket_maker_buy_rounds_down():
    # midpoint=63.5, floor=63
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "midmarket_maker") == 63


def test_midmarket_maker_sell_even_spread_stays_maker():
    # best_bid=62, best_ask=64 (100-36), midpoint=63.0, ceil=63, max(63, 63)=63 > bid 62
    assert place_order.compute_limit_price(_ob(62, 36), "sell", "midmarket_maker") == 63


def test_midmarket_maker_sell_spread_1_falls_back_to_join_ask():
    # best_bid=62, best_ask=63 (100-37), spread=1 → join_ask=63
    assert place_order.compute_limit_price(_ob(62, 37), "sell", "midmarket_maker") == 63


def test_midmarket_maker_buy_spread_1_falls_back_to_join_bid():
    # best_bid=62, best_ask=63, spread=1 → join_bid=62
    assert place_order.compute_limit_price(_ob(62, 37), "buy", "midmarket_maker") == 62


# --- join_ask / join_bid ---

def test_join_ask_returns_best_ask():
    # best_ask = 100 - 35 = 65
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "join_ask") == 65


def test_join_bid_returns_best_bid():
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "join_bid") == 62


# --- cross_spread ---

def test_cross_spread_sell_uses_best_bid():
    # sell at bid = immediate taker fill
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "cross_spread") == 62


def test_cross_spread_buy_uses_best_ask():
    # buy at ask = immediate taker fill
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "cross_spread") == 65


# --- error cases ---

def test_empty_yes_book_raises():
    ob = {"orderbook": {"yes": [], "no": [[35, 100]]}}
    with pytest.raises(ValueError, match="best_bid"):
        place_order.compute_limit_price(ob, "sell", "midmarket_maker")


def test_empty_no_book_raises():
    ob = {"orderbook": {"yes": [[62, 100]], "no": []}}
    with pytest.raises(ValueError, match="best_ask"):
        place_order.compute_limit_price(ob, "sell", "midmarket_maker")
