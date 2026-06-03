#!/usr/bin/env python3
"""Place a single limit sell order for a held Kalshi position.

Used by the portfolio review workflow to execute AI-recommended exits
after user approval. No position checks — places the order unconditionally.

Usage:
  KALSHI_ENV=prod PYTHONPATH=. python scripts/exit_position.py \
    --ticker KXFOO-25DEC01 --side yes --quantity 10 --yes-price 38

  # Dry-run (print intent, no order placed):
  KALSHI_ENV=prod PYTHONPATH=. python scripts/exit_position.py \
    --ticker KXFOO-25DEC01 --side yes --quantity 10 --yes-price 38 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient


async def exit_position(
    ticker: str,
    side: str,
    quantity: int,
    yes_price: int,
    dry_run: bool = False,
) -> dict:
    """Place a limit sell order for an open position. Returns result dict."""
    result = {
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "yes_price": yes_price,
        "dry_run": dry_run,
        "order_id": None,
        "order_status": None,
    }

    if dry_run:
        print(
            f"[DRY-RUN] Would sell {side.upper()} {quantity} contracts of "
            f"{ticker} at yes_price={yes_price}¢"
        )
        return result

    async with KalshiClient() as client:
        order_response = await client.create_order(
            ticker=ticker,
            action="sell",
            side=side,
            count=quantity,
            order_type="limit",
            yes_price=yes_price,
        )

    order_data = order_response.get("order", {})
    result["order_id"] = order_data.get("order_id")
    result["order_status"] = order_data.get("status")
    print(
        f"EXITED {ticker} {side.upper()} qty={quantity} "
        f"yes_price={yes_price}¢ order={result['order_id']}"
    )
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Place a limit sell order for a held position"
    )
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--side", required=True, choices=["yes", "no"])
    parser.add_argument("--quantity", required=True, type=int)
    parser.add_argument(
        "--yes-price",
        required=True,
        type=int,
        dest="yes_price",
        help="Limit price in cents (1-99), always in YES terms",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(
        exit_position(
            ticker=args.ticker,
            side=args.side,
            quantity=args.quantity,
            yes_price=args.yes_price,
            dry_run=args.dry_run,
        )
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _main()
