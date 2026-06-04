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
    """Return the YES-price limit (cents) for the exit order.

    YES stop_loss:       bid  (aggressive — immediate fill, guarantee exit)
    YES profit_target:   ask  (passive maker — fee-efficient, rest on book)
    NO  stop_loss:       ask  (aggressive NO sell crosses YES ask side)
    NO  profit_target:   bid  (passive NO sell rests at 100 - YES bid)
    """
    bid = orderbook_state.best_bid(ticker)
    ask = orderbook_state.best_ask(ticker)
    if bid is None or ask is None:
        return None
    if side == "yes":
        return bid if signal_reason == "stop_loss" else ask
    else:
        return ask if signal_reason == "stop_loss" else bid
