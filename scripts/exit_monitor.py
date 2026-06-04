"""Standalone real-time exit daemon.

Watches open Kalshi positions via WebSocket and fires deterministic
stop-loss / take-profit sells the moment a price threshold is crossed.
Run independently of the UI server and orchestrator loop.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import asyncio
import logging

import aiohttp

from kalshi_trader.client import KalshiClient
from kalshi_trader.external.kalshi_ws import KalshiWebSocketClient
from kalshi_trader.orderbook import OrderBookState
from kalshi_trader.portfolio_checks import EXIT_CHECKS
from kalshi_trader import db as _db
from kalshi_trader.dashboard.portfolio_mapping import parse_fixed_point

log = logging.getLogger("exit_monitor")


def _build_position_dict(
    position_meta: dict,
    orderbook_state: OrderBookState,
    ticker: str,
) -> dict | None:
    """Build the EXIT_CHECKS position dict from metadata + live WebSocket prices.

    Returns None if bid or ask is not yet available for the ticker.
    """
    bid = orderbook_state.best_bid(ticker)
    ask = orderbook_state.best_ask(ticker)
    if bid is None or ask is None:
        return None
    side = position_meta["side"]
    # Side-relative current price: what we'd receive selling this position now
    current_price_cents = float(bid) if side == "yes" else (100.0 - float(ask))
    midpoint_yes_price_cents = (float(bid) + float(ask)) / 2.0
    return {
        "market_exposure_dollars": position_meta["market_exposure_dollars"],
        "quantity": position_meta["quantity"],
        "current_price_cents": current_price_cents,
        "midpoint_yes_price_cents": midpoint_yes_price_cents,
    }


def _select_yes_price(
    signal_reason: str,
    side: str,
    orderbook_state: OrderBookState,
    ticker: str,
) -> int | None:
    """Return the value to pass as ``yes_price`` to ``create_order()`` for this exit.

    All four combos map to the raw order-book integer to pass directly to the API:

    YES stop_loss:       bid  (aggressive — immediate fill at YES bid)
    YES profit_target:   ask  (passive maker — rests at YES ask, fee-efficient)
    NO  stop_loss:       ask  (aggressive — yes_price=ask means NO is sold at 100-ask=NO_bid)
    NO  profit_target:   bid  (passive — yes_price=bid means NO rests at 100-bid=NO_ask)
    """
    bid = orderbook_state.best_bid(ticker)
    ask = orderbook_state.best_ask(ticker)
    if bid is None or ask is None:
        return None
    if side == "yes":
        return bid if signal_reason == "stop_loss" else ask
    else:
        return ask if signal_reason == "stop_loss" else bid


async def _fetch_open_positions(client: KalshiClient) -> dict[str, dict]:
    """Fetch open positions from Kalshi REST API.

    Returns {ticker: {ticker, side, quantity, market_exposure_dollars}}.
    Only includes positions with nonzero quantity.
    """
    response = await client.get_positions()
    raw_positions = response.get("market_positions") or []
    positions: dict[str, dict] = {}
    for raw in raw_positions:
        qty = parse_fixed_point(raw.get("position_fp"))
        if qty == 0:
            continue
        ticker = raw.get("ticker", "")
        if not ticker:
            continue
        positions[ticker] = {
            "ticker": ticker,
            "side": "yes" if qty > 0 else "no",
            "quantity": int(round(abs(qty))),
            "market_exposure_dollars": parse_fixed_point(raw.get("market_exposure_dollars")),
        }
    return positions


async def _fetch_resting_sell_tickers(client: KalshiClient) -> set[str]:
    """Return tickers that already have a resting sell order.

    Used on startup and refresh to pre-populate _pending_exits and prevent
    duplicate orders.
    """
    response = await client.get_orders(status="resting")
    orders = response.get("orders") or []
    return {
        o.get("ticker", "")
        for o in orders
        if o.get("action") == "sell" and o.get("ticker")
    }
