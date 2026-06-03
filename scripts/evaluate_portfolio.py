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
        ticker: {"yes_bid": None, "yes_ask": None} for ticker in tickers
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
                }
    return prices


async def run(dry_run: bool, out: str | None) -> dict:
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
        live_prices = await _fetch_live_prices(client, tickers)

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
                clean_positions.append({"ticker": ticker, "side": side, "description": "within bounds"})
                continue

            yes_price = max(1, min(99, round(exit_signal.exit_price_cents)))
            order_id = None
            order_status = None

            if not dry_run:
                try:
                    order_response = await client.create_order(
                        ticker=ticker,
                        action="sell",
                        side=side,
                        count=int(quantity),
                        order_type="limit",
                        yes_price=yes_price,
                    )
                    order_data = order_response.get("order", {})
                    order_id = order_data.get("order_id")
                    order_status = order_data.get("status")
                    print(
                        f"EXITED {ticker} {side.upper()} qty={int(quantity)} "
                        f"price={yes_price}¢ ({exit_signal.description}) order={order_id}"
                    )
                except Exception as caught_exception:
                    errors.append({"ticker": ticker, "error": str(caught_exception)})
                    print(f"ERROR {ticker}: {caught_exception}", file=sys.stderr)
                    continue
            else:
                print(
                    f"[DRY-RUN] Would exit {ticker} {side.upper()} qty={int(quantity)} "
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute triggers but place no orders")
    parser.add_argument("--out", default=None,
                        help="Write results JSON to this path (for orchestrator)")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, out=args.out))


if __name__ == "__main__":
    _main()
