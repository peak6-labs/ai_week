#!/usr/bin/env python
"""Fetch Kalshi settlement rules for one or more markets (read-only).

The bulk market snapshot omits rules; the per-market endpoint includes them. The
pipeline uses this to verify that a market settles on what its title implies, and
that any matched external contract (Polymarket / sportsbook) is truly equivalent
— guarding against "looks the same, settles differently" mismatches.

Usage:
    KALSHI_ENV=prod python scripts/market_rules.py --tickers KXFOO-1 KXBAR-2 [...]

Prints a JSON object: { ticker: {rules_primary, rules_secondary, subtitle,
yes_sub_title, no_sub_title} }. Bounded concurrency; never raises per ticker.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient

_FIELDS = ("rules_primary", "rules_secondary", "subtitle", "yes_sub_title", "no_sub_title")


async def _run(tickers: list[str]) -> None:
    client = KalshiClient()
    semaphore = asyncio.Semaphore(8)

    async def fetch(ticker: str) -> tuple[str, dict]:
        async with semaphore:
            try:
                response = await client.get_market(ticker)
                market = response.get("market", response)
                return ticker, {field: market.get(field) for field in _FIELDS}
            except Exception as exc:  # one bad ticker shouldn't sink the batch
                return ticker, {"error": str(exc)[:120]}

    results = await asyncio.gather(*[fetch(t) for t in tickers])
    if hasattr(client, "close"):
        await client.close()
    print(json.dumps({ticker: rules for ticker, rules in results}, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Kalshi market settlement rules")
    parser.add_argument("--tickers", nargs="+", required=True)
    args = parser.parse_args()
    if not args.tickers:
        print("{}")
        return
    asyncio.run(_run(args.tickers))


if __name__ == "__main__":
    main()
