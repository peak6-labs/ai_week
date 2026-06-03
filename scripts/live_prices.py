#!/usr/bin/env python
"""Fetch live prices for a set of tickers in a single batched call.

The market snapshot is used only for the market *universe*; prices for any market
we actually evaluate must come from the live API. The Kalshi ``/markets`` endpoint
accepts a comma-separated ``tickers`` filter and returns them all in one request —
and ``KalshiClient.get_markets`` already normalizes the prod schema to canonical
cents. So this is one API call, not one per ticker.

    KALSHI_ENV=prod PYTHONPATH=. python scripts/live_prices.py \
        --tickers KXFOO-A KXBAR-B > /tmp/live_prices.json

Output: {"KXFOO-A": {"yes_bid": 26.0, "yes_ask": 30.0, "last_price": 28.0}, ...}
A ticker the API does not return (illiquid/closed) maps to nulls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient

# The /markets tickers filter, like the candlesticks batch, is bounded; chunk to
# stay well under the limit when a subset is large.
_BATCH = 100


async def fetch_live_prices(tickers: list[str]) -> dict[str, dict]:
    prices: dict[str, dict] = {
        ticker: {"yes_bid": None, "yes_ask": None, "last_price": None} for ticker in tickers
    }
    async with KalshiClient() as client:
        for start in range(0, len(tickers), _BATCH):
            chunk = tickers[start:start + _BATCH]
            response = await client.get_markets(tickers=",".join(chunk), limit=_BATCH)
            for market in response.get("markets") or []:
                ticker = market.get("ticker")
                if ticker in prices:
                    prices[ticker] = {
                        "yes_bid": market.get("yes_bid"),
                        "yes_ask": market.get("yes_ask"),
                        "last_price": market.get("last_price"),
                    }
    return prices


async def _main(tickers: list[str]) -> None:
    prices = await fetch_live_prices(tickers)
    print(json.dumps(prices))
    quoted = sum(1 for value in prices.values() if value["yes_ask"] is not None)
    print(f"live-priced {quoted}/{len(tickers)} tickers ({len(tickers) - quoted} unquoted)",
          file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch live prices for tickers (batched)")
    parser.add_argument("--tickers", nargs="+", required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.tickers))
