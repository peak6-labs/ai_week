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


async def _notify_server(message: str, level: str = "info") -> None:
    """Fire-and-forget POST to the dashboard's /api/log endpoint.

    Silently swallowed if the server is not running.
    """
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                "http://localhost:8000/api/log",
                json={"message": message, "level": level},
                timeout=aiohttp.ClientTimeout(total=2.0),
            )
    except Exception:
        pass


async def run(dry_run: bool) -> None:
    """Main exit monitor loop. Runs until interrupted."""
    log.info("Exit monitor starting (dry_run=%s)", dry_run)

    async with KalshiClient() as kalshi_client:
        orderbook_state = OrderBookState()
        position_metadata: dict[str, dict] = {}
        pending_exits: set[str] = set()
        ws_client: KalshiWebSocketClient | None = None
        ws_task: asyncio.Task | None = None
        subscribed_tickers: set[str] = set()

        async def _refresh() -> None:
            nonlocal ws_client, ws_task, subscribed_tickers

            new_positions = await _fetch_open_positions(kalshi_client)
            position_metadata.clear()
            position_metadata.update(new_positions)

            # Remove pending exits for positions that are gone (exit filled)
            for ticker in list(pending_exits):
                if ticker not in position_metadata:
                    pending_exits.discard(ticker)

            # Pre-populate pending_exits from any resting sell orders
            resting_sell_tickers = await _fetch_resting_sell_tickers(kalshi_client)
            pending_exits.update(resting_sell_tickers & set(position_metadata))

            # Update WebSocket subscriptions if positions changed
            new_tickers = set(position_metadata)
            if new_tickers != subscribed_tickers:
                if ws_client is not None:
                    await ws_client.stop()
                if ws_task is not None and not ws_task.done():
                    ws_task.cancel()
                    try:
                        await ws_task
                    except asyncio.CancelledError:
                        pass

                subscribed_tickers = new_tickers
                if subscribed_tickers:
                    ws_client = KalshiWebSocketClient(list(subscribed_tickers), orderbook_state)
                    ws_task = asyncio.create_task(ws_client.run())
                    log.info("WebSocket subscribed to %d tickers: %s",
                             len(subscribed_tickers), sorted(subscribed_tickers))
                else:
                    ws_client = None
                    ws_task = None
                    log.info("No open positions — WebSocket not active")

        await _refresh()
        last_refresh_time = asyncio.get_event_loop().time()

        while True:
            now = asyncio.get_event_loop().time()
            if now - last_refresh_time >= 30.0:
                try:
                    await _refresh()
                except Exception as refresh_exc:
                    log.warning("Position refresh failed: %s", refresh_exc)
                last_refresh_time = now

            # Detect silent WebSocket task death and log loudly
            if ws_task is not None and ws_task.done():
                exc = ws_task.exception() if not ws_task.cancelled() else None
                log.error(
                    "WebSocket task died unexpectedly (exc=%s) — "
                    "prices are stale, exit checks suspended until next refresh",
                    exc,
                )
                ws_task = None  # cleared so _refresh() will restart it

            for ticker, meta in list(position_metadata.items()):
                if ticker in pending_exits:
                    continue

                position_dict = _build_position_dict(meta, orderbook_state, ticker)
                if position_dict is None:
                    continue  # WebSocket not yet priced this ticker

                for check in EXIT_CHECKS:
                    signal = check(position_dict)
                    if signal is None:
                        continue

                    yes_price = _select_yes_price(
                        signal.reason, meta["side"], orderbook_state, ticker
                    )
                    if yes_price is None:
                        break

                    pending_exits.add(ticker)
                    msg = (
                        f"{signal.reason} fired: {ticker} ({meta['side'].upper()}) "
                        f"at {yes_price}¢ — {signal.description}"
                    )
                    log.warning(msg)

                    if dry_run:
                        log.info("DRY-RUN: would sell %s %s %dx at yes_price=%d¢",
                                 meta["side"].upper(), ticker, meta["quantity"], yes_price)
                    else:
                        try:
                            order_response = await kalshi_client.create_order(
                                ticker=ticker,
                                action="sell",
                                side=meta["side"],
                                count=meta["quantity"],
                                order_type="limit",
                                yes_price=yes_price,
                            )
                            order_id = order_response.get("order", {}).get("order_id")
                            log.info("Exit order placed: order_id=%s", order_id)

                            if order_id:
                                realized_pnl = (
                                    yes_price / 100.0 * meta["quantity"]
                                    - meta["market_exposure_dollars"]
                                )
                                try:
                                    await _db.close_position(
                                        ticker=ticker,
                                        closing_trade_id=order_id,
                                        realized_pnl_dollars=realized_pnl,
                                        exit_reason=signal.reason,
                                    )
                                except Exception as db_exc:
                                    log.debug("Supabase close_position skipped: %s", db_exc)
                        except Exception as order_exc:
                            log.error("Exit order failed for %s: %s", ticker, order_exc)
                            pending_exits.discard(ticker)

                    await _notify_server(msg, "warning")
                    break  # only first signal per ticker per loop iteration

            await asyncio.sleep(0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real-time deterministic exit monitor for open Kalshi positions."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log exit signals but do not place orders or write to Supabase",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(dry_run=args.dry_run))
