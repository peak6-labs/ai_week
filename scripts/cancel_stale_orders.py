#!/usr/bin/env python3
"""Cancel resting orders older than --minutes (default 10).

Outputs a JSON array to stdout. Each element is a cancelled order with fields:
  order_id, ticker, action, side, yes_price_dollars, remaining_count_fp, cancelled

action="sell" orders are exit orders — the caller decides whether to replace them.
action="buy"  orders are entry orders — the caller should NOT replace them.

Usage:
  KALSHI_ENV=prod PYTHONPATH=. python scripts/cancel_stale_orders.py
  KALSHI_ENV=prod PYTHONPATH=. python scripts/cancel_stale_orders.py --minutes 15
  KALSHI_ENV=prod PYTHONPATH=. python scripts/cancel_stale_orders.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient


def _parse_created_time(created_time: str) -> datetime:
    return datetime.fromisoformat(created_time.replace("Z", "+00:00"))


async def run(stale_minutes: int = 10, dry_run: bool = False) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)

    async with KalshiClient() as client:
        response = await client.get_orders(status="resting")
        orders = response.get("orders", [])

        stale_orders = [
            order for order in orders
            if _parse_created_time(order["created_time"]) < cutoff
        ]

        results = []
        for order in stale_orders:
            order_id = order["order_id"]
            ticker = order["ticker"]
            action = order["action"]
            record = {
                "order_id": order_id,
                "ticker": ticker,
                "action": action,
                "side": order.get("side"),
                "yes_price_dollars": order.get("yes_price_dollars"),
                "remaining_count_fp": order.get("remaining_count_fp"),
                "created_time": order["created_time"],
                "cancelled": False,
            }
            if dry_run:
                print(
                    f"[DRY-RUN] Would cancel {action.upper()} order {order_id} "
                    f"on {ticker} (age > {stale_minutes}m)",
                    file=sys.stderr,
                )
                record["cancelled"] = True
            else:
                try:
                    await client.cancel_order(order_id)
                    record["cancelled"] = True
                    print(
                        f"CANCELLED {action.upper()} order {order_id} on {ticker}",
                        file=sys.stderr,
                    )
                except Exception as caught_exception:
                    print(
                        f"ERROR cancelling {order_id} on {ticker}: {caught_exception}",
                        file=sys.stderr,
                    )
            results.append(record)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel stale resting orders")
    parser.add_argument("--minutes", type=int, default=10,
                        help="Age threshold in minutes (default: 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be cancelled without cancelling")
    args = parser.parse_args()

    results = asyncio.run(run(stale_minutes=args.minutes, dry_run=args.dry_run))
    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
