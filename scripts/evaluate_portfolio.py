#!/usr/bin/env python3
"""Evaluate open Kalshi positions and exit any that breach stop-loss or profit-target.

Fetches live positions and top-of-book prices, runs each position through
EXIT_CHECKS, and immediately places limit sell orders for any that trigger.
Exit logic is deterministic — no model reasoning involved.

Usage:
  # Live (places orders for triggered positions):
  KALSHI_ENV=prod PYTHONPATH=. python scripts/evaluate_portfolio.py

  # Dry-run (compute triggers, place no orders):
  KALSHI_ENV=prod PYTHONPATH=. python scripts/evaluate_portfolio.py --dry-run

  # Write results JSON for the orchestrator to read:
  KALSHI_ENV=prod PYTHONPATH=. python scripts/evaluate_portfolio.py \\
    --out /tmp/portfolio_eval_20260603T120000Z.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient
from kalshi_trader.dashboard.portfolio_mapping import parse_fixed_point
from kalshi_trader.portfolio_checks import EXIT_CHECKS

_PRICE_BATCH = 100  # max tickers per /markets request


async def _fetch_live_prices(client: KalshiClient, tickers: list[str]) -> dict[str, dict]:
    """Fetch live yes_bid/yes_ask for each ticker in one batched /markets call."""
    prices: dict[str, dict] = {
        ticker: {
            "yes_bid": None, "yes_ask": None,
            "title": None, "close_time": None, "volume_24h": None,
        }
        for ticker in tickers
    }
    for start in range(0, len(tickers), _PRICE_BATCH):
        chunk = tickers[start : start + _PRICE_BATCH]
        response = await client.get_markets(tickers=",".join(chunk), limit=_PRICE_BATCH)
        for market in response.get("markets") or []:
            ticker = market.get("ticker")
            if ticker in prices:
                prices[ticker] = {
                    "yes_bid": market.get("yes_bid"),
                    "yes_ask": market.get("yes_ask"),
                    "title": market.get("title"),
                    "close_time": market.get("close_time"),
                    "volume_24h": market.get("volume_24h"),
                }
    return prices


async def _fetch_tickers_with_resting_exits(client: KalshiClient) -> set[str]:
    """Return the set of tickers that already have a resting sell order."""
    response = await client.get_orders(status="resting")
    return {
        order["ticker"]
        for order in (response.get("orders") or [])
        if order.get("action") == "sell"
    }


async def run(dry_run: bool, out: str | None, night_mode: bool = False) -> dict:
    """Evaluate all open positions and exit triggered ones. Returns results dict."""
    async with KalshiClient() as client:
        positions_response = await client.get_positions()

        raw_positions = [
            position_raw for position_raw in (positions_response.get("market_positions") or [])
            if parse_fixed_point(position_raw.get("position_fp")) != 0
        ]

        if not raw_positions:
            results = {
                "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "total_positions": 0,
                "triggered_count": 0,
                "dry_run": dry_run,
                "exits": [],
                "clean_positions": [],
                "errors": [],
            }
            _write_out(results, out)
            return results

        tickers = [position_raw.get("ticker", "") for position_raw in raw_positions if position_raw.get("ticker")]
        live_prices, tickers_with_resting_exit = await asyncio.gather(
            _fetch_live_prices(client, tickers),
            _fetch_tickers_with_resting_exits(client),
        )

        exits: list[dict] = []
        clean_positions: list[dict] = []
        errors: list[dict] = []

        for position_raw in raw_positions:
            ticker = position_raw.get("ticker", "")
            prices = live_prices.get(ticker, {})
            yes_bid = prices.get("yes_bid")
            yes_ask = prices.get("yes_ask")

            if yes_bid is None or yes_ask is None:
                errors.append({"ticker": ticker, "error": "no live price available"})
                print(f"SKIP {ticker}: no live price", file=sys.stderr)
                continue

            midpoint_yes_price_cents = (yes_bid + yes_ask) / 2.0
            signed_quantity = parse_fixed_point(position_raw.get("position_fp"))
            side = "yes" if signed_quantity >= 0 else "no"
            quantity = abs(signed_quantity)
            current_price_cents = (
                midpoint_yes_price_cents if side == "yes" else (100.0 - midpoint_yes_price_cents)
            )
            market_exposure_dollars = parse_fixed_point(position_raw.get("market_exposure_dollars"))

            position = {
                "ticker": ticker,
                "side": side,
                "quantity": quantity,
                "market_exposure_dollars": market_exposure_dollars,
                "current_price_cents": current_price_cents,
                "midpoint_yes_price_cents": midpoint_yes_price_cents,
            }

            exit_signal = None
            for check in EXIT_CHECKS:
                exit_signal = check(position)
                if exit_signal is not None:
                    break

            if exit_signal is None:
                market_info = live_prices.get(ticker, {})
                average_price_cents = (
                    round(market_exposure_dollars / quantity * 100.0, 2) if quantity > 0 else None
                )
                unrealized_pnl_dollars = round(
                    quantity * current_price_cents / 100.0 - market_exposure_dollars, 2
                )
                clean_positions.append({
                    "ticker": ticker,
                    "side": side,
                    "quantity": quantity,
                    "avg_price_cents": average_price_cents,
                    "current_price_cents": round(current_price_cents, 2),
                    "midpoint_yes_price_cents": round(midpoint_yes_price_cents, 2),
                    "market_exposure_dollars": round(market_exposure_dollars, 2),
                    "unrealized_pnl_dollars": unrealized_pnl_dollars,
                    "title": market_info.get("title"),
                    "close_time": (
                        str(market_info.get("close_time")) if market_info.get("close_time") else None
                    ),
                    "volume_24h": market_info.get("volume_24h"),
                    "description": "within bounds",
                })
                continue

            if ticker in tickers_with_resting_exit:
                print(
                    f"SKIP {ticker}: resting exit order already exists",
                    file=sys.stderr,
                )
                continue

            exit_quantity = int(quantity)
            if exit_quantity < 1:
                print(f"SKIP {ticker}: fractional position ({quantity:.2f} contracts)", file=sys.stderr)
                continue

            # Stop-losses always cross the spread to guarantee a fill even when prices are ripping.
            # Profit targets rest at midmarket (maker, no fees) unless --night-mode is set,
            # in which case they also cross the spread so exits complete without human oversight.
            if exit_signal.reason == "stop_loss" or night_mode:
                # Taker pricing: sell YES at bid, sell NO at ask (both cross immediately).
                taker_yes_price = yes_bid if side == "yes" else yes_ask
                yes_price = max(1, min(99, round(taker_yes_price)))
            else:
                yes_price = max(1, min(99, round(exit_signal.exit_price_cents)))
            order_id = None
            order_status = None

            if not dry_run:
                try:
                    order_response = await client.create_order(
                        ticker=ticker,
                        action="sell",
                        side=side,
                        count=exit_quantity,
                        order_type="limit",
                        yes_price=yes_price,
                    )
                    order_data = order_response.get("order", {})
                    order_id = order_data.get("order_id")
                    order_status = order_data.get("status")
                    print(
                        f"EXITED {ticker} {side.upper()} qty={exit_quantity} "
                        f"price={yes_price}¢ ({exit_signal.description}) order={order_id}"
                    )
                    # Space consecutive placements to stay under the 429 rate limit.
                    await asyncio.sleep(kalshi_trader.config.INTER_ORDER_DELAY_SECONDS)
                except Exception as caught_exception:
                    errors.append({"ticker": ticker, "error": str(caught_exception)})
                    print(f"ERROR {ticker}: {caught_exception}", file=sys.stderr)
                    continue
            else:
                print(
                    f"[DRY-RUN] Would exit {ticker} {side.upper()} qty={exit_quantity} "
                    f"price={yes_price}¢ ({exit_signal.description})"
                )

            exits.append({
                "ticker": ticker,
                "side": side,
                "quantity": quantity,
                "reason": exit_signal.reason,
                "description": exit_signal.description,
                "exit_price_cents": yes_price,
                "order_id": order_id,
                "order_status": order_status,
            })

        results = {
            "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "total_positions": len(raw_positions),
            "triggered_count": len(exits),
            "dry_run": dry_run,
            "exits": exits,
            "clean_positions": clean_positions,
            "errors": errors,
        }
    _write_out(results, out)
    return results


def _write_out(results: dict, out: str | None) -> None:
    if out:
        Path(out).write_text(json.dumps(results, indent=2, default=str))


def _main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate positions and exit triggered ones")
    parser.add_argument("--execute", action="store_true",
                        help="Actually place exit orders. DEFAULT IS DRY-RUN (place nothing). "
                        "Even with --execute, the client still requires KALSHI_ALLOW_ORDERS=1.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Force dry-run (the default); kept for clarity/back-compat.")
    parser.add_argument("--out", default=None,
                        help="Write results JSON to this path (for orchestrator)")
    parser.add_argument("--night-mode", action="store_true",
                        help="Use taker pricing for profit-target exits so they fill without human oversight.")
    args = parser.parse_args()
    # Dry-run unless execution is explicitly requested; --dry-run also forces it.
    dry_run = args.dry_run or not args.execute
    asyncio.run(run(dry_run=dry_run, out=args.out, night_mode=args.night_mode))


if __name__ == "__main__":
    _main()
