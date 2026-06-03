"""Tests for paper-trade P&L math (kalshi_trader/paper.py)."""
from __future__ import annotations

from kalshi_trader import paper


def test_entry_price_yes_is_ask_no_is_complement_of_bid() -> None:
    assert paper.entry_price_cents("yes", yes_bid=38, yes_ask=40) == 40
    assert paper.entry_price_cents("no", yes_bid=38, yes_ask=40) == 62  # 100-38


def test_mark_value_sells_into_the_book() -> None:
    # YES marked at the bid you could sell into; NO at 100-ask.
    assert paper.mark_value_cents("yes", yes_bid=44, yes_ask=46) == 44
    assert paper.mark_value_cents("no", yes_bid=44, yes_ask=46) == 54


def test_settle_value_pays_100_to_the_winning_side() -> None:
    assert paper.settle_value_cents("yes", resolved_yes=True) == 100
    assert paper.settle_value_cents("yes", resolved_yes=False) == 0
    assert paper.settle_value_cents("no", resolved_yes=False) == 100
    assert paper.settle_value_cents("no", resolved_yes=True) == 0


def test_compute_mark_unresolved_profit() -> None:
    # Bought YES at 40, market now 50/52 → sell into 50 bid → +10c.
    mark = paper.compute_mark("yes", entry_cents=40, yes_bid=50, yes_ask=52, resolved_yes=None)
    assert mark["resolved"] is False
    assert mark["pnl_cents"] == 10
    assert mark["would_profit"] is True


def test_compute_mark_resolved_winner() -> None:
    mark = paper.compute_mark("no", entry_cents=62, yes_bid=0, yes_ask=0, resolved_yes=False)
    assert mark["resolved"] is True
    assert mark["current_value_cents"] == 100
    assert mark["pnl_cents"] == 38


def test_compute_mark_no_price_unresolved_returns_none() -> None:
    mark = paper.compute_mark("yes", entry_cents=40, yes_bid=None, yes_ask=None, resolved_yes=None)
    assert mark["pnl_cents"] is None
    assert mark["resolved"] is False
