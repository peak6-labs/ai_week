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
    yes_bid = orderbook_state.best_bid(ticker)      # max YES book = YES bid
    no_bid = orderbook_state.best_no_bid(ticker)    # max NO book  = NO bid
    if yes_bid is None or no_bid is None:
        return None
    side = position_meta["side"]
    # Side-relative current price: what we'd receive selling this position now
    current_price_cents = float(yes_bid) if side == "yes" else float(no_bid)
    # YES midpoint = (YES bid + YES ask) / 2 = (yes_bid + (100 - no_bid)) / 2
    midpoint_yes_price_cents = (float(yes_bid) + (100.0 - float(no_bid))) / 2.0
    result = {
        "market_exposure_dollars": position_meta["market_exposure_dollars"],
        "quantity": position_meta["quantity"],
        "current_price_cents": current_price_cents,
        "midpoint_yes_price_cents": midpoint_yes_price_cents,
    }
    if position_meta.get("fair_value_cents") is not None:
        # predicted_prob is the probability of the recommended side, so fair_value_cents
        # is already in side-relative terms — no conversion needed.
        result["fair_value_cents"] = position_meta["fair_value_cents"]
    return result


def _select_yes_price(
    signal_reason: str,
    side: str,
    orderbook_state: OrderBookState,
    ticker: str,
) -> int | None:
    """Return the value to pass as ``yes_price`` to ``create_order()`` for this exit.

    yes_bid = max(YES book) = YES bid = price to sell YES aggressively
    no_bid  = max(NO book)  = NO bid  = price to sell NO aggressively

    YES stop_loss      → yes_price = yes_bid         (sell YES at bid, immediate fill)
    YES profit_target  → yes_price = 100 - no_bid    (= YES ask, passive maker)
    NO  stop_loss      → yes_price = 100 - no_bid    (sell NO at NO bid, immediate fill)
    NO  profit_target  → yes_price = yes_bid         (sell NO at 100-yes_bid = NO ask, passive maker)
    """
    yes_bid = orderbook_state.best_bid(ticker)
    no_bid = orderbook_state.best_no_bid(ticker)
    if yes_bid is None or no_bid is None:
        return None
    yes_ask = 100 - no_bid
    if side == "yes":
        raw = yes_bid if signal_reason == "stop_loss" else yes_ask
    else:
        raw = yes_ask if signal_reason == "stop_loss" else yes_bid
    # Clamp to valid Kalshi range — 0 or 100 are rejected; near-settled books hit these extremes.
    return max(1, min(99, raw))


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
        rounded_quantity = int(round(abs(qty)))
        if rounded_quantity == 0:
            continue
        ticker = raw.get("ticker", "")
        if not ticker:
            continue
        positions[ticker] = {
            "ticker": ticker,
            "side": "yes" if qty > 0 else "no",
            "quantity": rounded_quantity,
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
        if o.get("action") == "sell"
        and o.get("ticker")
        and parse_fixed_point(o.get("remaining_count_fp", "0")) > 0
    }


async def _notify_server(message: str, level: str = "info") -> None:
    """Fire-and-forget POST to the dashboard's /api/log endpoint.

    Silently swallowed if the server is not running.
    """
    connector = aiohttp.TCPConnector()
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            await session.post(
                "http://localhost:8000/api/log",
                json={"message": message, "level": level},
                timeout=aiohttp.ClientTimeout(total=2.0),
            )
    except Exception:
        pass
    finally:
        await connector.close()


async def run(dry_run: bool) -> None:
    """Main exit monitor loop. Runs until interrupted."""
    log.info("Exit monitor starting (dry_run=%s)", dry_run)

    async with KalshiClient() as kalshi_client:
        orderbook_state = OrderBookState()
        position_metadata: dict[str, dict] = {}
        pending_exits: set[str] = set()
        new_this_cycle: set[str] = set()   # tickers that just appeared; skip for one cycle
        ws_client: KalshiWebSocketClient | None = None
        ws_task: asyncio.Task | None = None
        subscribed_tickers: set[str] = set()

        async def _refresh() -> None:
            nonlocal ws_client, ws_task, subscribed_tickers, new_this_cycle

            new_positions = await _fetch_open_positions(kalshi_client)

            # Enrich each position with fair value from the recommendations table
            if new_positions:
                try:
                    fair_values = await _db.get_fair_values_from_recommendations(list(new_positions))
                    enriched, missing = [], []
                    for ticker, meta in new_positions.items():
                        prob = fair_values.get(ticker)
                        if prob is not None:
                            meta["fair_value_cents"] = round(prob * 100.0, 2)
                            enriched.append(f"{ticker}={prob*100:.1f}¢")
                        else:
                            missing.append(ticker)
                    if enriched:
                        log.info("Fair values loaded: %s", ", ".join(enriched))
                    if missing:
                        log.info("No fair value in recommendations (using convergence fallback): %s",
                                 ", ".join(missing))
                except Exception as fair_value_exception:
                    log.warning("Fair value lookup failed: %s", fair_value_exception)

            # Track tickers that are brand new this cycle — skip exit checks for one cycle
            # so a position we just entered isn't immediately exited before it has a chance to move.
            new_this_cycle = set(new_positions) - set(position_metadata)
            if new_this_cycle:
                log.info("New position(s) entered — skipping exit checks for one cycle: %s",
                         sorted(new_this_cycle))

            position_metadata.clear()
            position_metadata.update(new_positions)

            # Remove pending exits for positions that are gone (exit filled)
            for ticker in list(pending_exits):
                if ticker not in position_metadata:
                    pending_exits.discard(ticker)

            # Pre-populate pending_exits from any resting sell orders
            resting_sell_tickers = await _fetch_resting_sell_tickers(kalshi_client)
            pending_exits.update(resting_sell_tickers & set(position_metadata))

            # Sync orderbook state from REST on every refresh — corrects any WebSocket
            # delta drift so stop-loss checks always use exchange-accurate prices.
            # Small inter-fetch delay prevents bursting all positions at once.
            for ticker in list(position_metadata):
                try:
                    fresh_orderbook_response = await kalshi_client.get_orderbook(ticker)
                    fresh_orderbook = fresh_orderbook_response.get("orderbook", {})
                    orderbook_state.apply_snapshot(
                        ticker,
                        fresh_orderbook.get("yes", []),
                        fresh_orderbook.get("no", []),
                    )
                except Exception as orderbook_sync_exception:
                    log.debug("Orderbook REST sync failed for %s: %s", ticker, orderbook_sync_exception)
                await asyncio.sleep(0.25)

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
                    log.info("WebSocket subscribed to %d tickers", len(subscribed_tickers))
                else:
                    ws_client = None
                    ws_task = None
                    log.info("No open positions — WebSocket not active")

        await _refresh()
        last_refresh_time = asyncio.get_running_loop().time()
        exits_since_refresh = 0

        while True:
            now = asyncio.get_running_loop().time()
            if now - last_refresh_time >= 30.0:
                if exits_since_refresh == 0:
                    log.info("No exits triggered — monitoring %d positions", len(position_metadata))
                exits_since_refresh = 0
                try:
                    await _refresh()
                except Exception as refresh_exception:
                    log.warning("Position refresh failed: %s", refresh_exception)
                last_refresh_time = now

            # Detect silent WebSocket task death and log loudly
            if ws_task is not None and ws_task.done():
                ws_task_exception = ws_task.exception() if not ws_task.cancelled() else None
                log.error(
                    "WebSocket task died unexpectedly (exc=%s) — "
                    "prices are stale, exit checks suspended until next refresh",
                    ws_task_exception,
                )
                ws_task = None  # cleared so _refresh() will restart it

            for ticker, meta in list(position_metadata.items()):
                if ticker in pending_exits:
                    continue
                if ticker in new_this_cycle:
                    continue  # just entered; wait one cycle before checking exits

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
                    exits_since_refresh += 1
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
                                except Exception as database_exception:
                                    log.debug("Supabase close_position skipped: %s", database_exception)
                        except Exception as order_exception:
                            response_body = ""
                            raw_response = getattr(order_exception, "response", None)
                            if raw_response is not None:
                                try:
                                    response_body = f" — {raw_response.text}"
                                except Exception:
                                    pass
                            log.error("Exit order failed for %s: %s%s", ticker, order_exception, response_body)
                            pending_exits.discard(ticker)

                    await _notify_server(msg, "warning")
                    await asyncio.sleep(0.5)  # pace consecutive exit orders
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
    parser.add_argument(
        "--log-file",
        default="logs/exit_monitor.log",
        help="Path to log file (default: logs/exit_monitor.log)",
    )
    args = parser.parse_args()

    log_format = "%(asctime)s  %(levelname)-7s  %(name)s — %(message)s"
    log_datefmt = "%Y-%m-%d %H:%M:%S"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(logging.Formatter(log_format, datefmt=log_datefmt))
    root_logger.addHandler(stdout_handler)

    log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.log_file) \
        if not os.path.isabs(args.log_file) else args.log_file
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=log_datefmt))
    root_logger.addHandler(file_handler)

    log.info("Logging to %s", log_path)
    asyncio.run(run(dry_run=args.dry_run))
