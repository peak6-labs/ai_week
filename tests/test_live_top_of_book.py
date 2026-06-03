"""Deriving live top-of-book bid/ask from a Kalshi orderbook.

The orderbook holds resting bids on each side: a YES bid at price P and a NO bid
at price Q (cents). The best YES bid is the highest YES level; the best YES ask
is 100 minus the highest NO bid (buying YES = selling NO). This lets the scorer
replace stale snapshot prices with the live book at score time.
"""
from kalshi_trader.actionability.signals import live_top_of_book


def test_derives_yes_bid_and_ask_from_book():
    orderbook = {
        "yes": [[80, 200.0], [81, 72.0], [82, 57.0]],
        "no": [[10, 300.0], [12, 17201.0], [13, 7797.0]],
    }
    yes_bid, yes_ask = live_top_of_book(orderbook)
    assert yes_bid == 82.0          # highest yes level
    assert yes_ask == 87.0          # 100 - highest no level (13)


def test_returns_none_when_a_side_is_empty():
    assert live_top_of_book({"yes": [], "no": [[13, 100.0]]}) == (None, 87.0)
    assert live_top_of_book({"yes": [[82, 1.0]], "no": []}) == (82.0, None)


def test_returns_none_for_empty_or_missing_book():
    assert live_top_of_book({}) == (None, None)
    assert live_top_of_book({"yes": [], "no": []}) == (None, None)
