#!/usr/bin/env python3
"""Place, cancel, or cancel-and-replace Kalshi orders via natural language or structured flags.

Usage:
  python scripts/place_order.py --ticker KXATL-26JUN-A1 "exit full position at midmarket no fees"
  python scripts/place_order.py --ticker KXATL-26JUN-A1 "cancel and replace at 65 cents"
  python scripts/place_order.py --ticker KXATL-26JUN-A1 "i need to get filled"
  python scripts/place_order.py --ticker KXATL-26JUN-A1 --action sell --quantity all --pricing midmarket_maker
  python scripts/place_order.py --ticker KXATL-26JUN-A1 "cancel and replace at 65" --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env


def compute_limit_price(orderbook_data: dict, action: str, pricing: str) -> int:
    """Compute a yes_price (1-99 cents) for a limit order given a pricing strategy.

    orderbook_data: normalized dict from KalshiClient.get_orderbook() — must contain
        {"orderbook": {"yes": [[price_cents, qty], ...], "no": [[price_cents, qty], ...]}}
    action: "buy" or "sell"
    pricing: "midmarket_maker" | "join_bid" | "join_ask" | "cross_spread"

    Maker strategies (midmarket_maker, join_bid, join_ask) never cross the spread.
    cross_spread crosses immediately — taker fees apply.
    Raises ValueError if either side of the book is empty.
    """
    ob = orderbook_data.get("orderbook", {})
    yes_prices = [lvl[0] for lvl in ob.get("yes", []) if lvl]
    no_prices = [lvl[0] for lvl in ob.get("no", []) if lvl]

    if not yes_prices:
        raise ValueError("best_bid unavailable — YES book is empty. Use --yes-price to set price explicitly.")
    if not no_prices:
        raise ValueError("best_ask unavailable — NO book is empty. Use --yes-price to set price explicitly.")

    best_bid = max(yes_prices)
    best_ask = 100 - max(no_prices)

    if pricing == "join_bid":
        return best_bid
    if pricing == "join_ask":
        return best_ask
    if pricing == "cross_spread":
        return best_bid if action == "sell" else best_ask
    # midmarket_maker (default)
    spread = best_ask - best_bid
    if spread <= 1:
        return best_ask if action == "sell" else best_bid
    midpoint = (best_bid + best_ask) / 2.0
    if action == "sell":
        return max(math.ceil(midpoint), best_bid + 1)
    return min(math.floor(midpoint), best_ask - 1)
