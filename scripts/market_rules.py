#!/usr/bin/env python
"""Fetch Kalshi settlement rules for one or more markets (read-only).

The bulk market snapshot omits rules; the per-market endpoint includes them. The
pipeline uses this to verify that a market settles on what its title implies, and
that any matched external contract (Polymarket / sportsbook) is truly equivalent
— guarding against "looks the same, settles differently" mismatches.

Usage:
    KALSHI_ENV=prod python scripts/market_rules.py --tickers KXFOO-1 KXBAR-2 [...]

Prints a JSON object: { ticker: {rules_primary, rules_secondary, subtitle,
yes_sub_title, no_sub_title, settlement_sources, contract_terms_url} }. The last
two come from the market's **series** (one lookup per distinct series, cached in
series_contract_terms.json — a strike ladder costs one call, zero next cycle) so
the pipeline knows what source/criterion the contract actually settles on.
Bounded concurrency; never raises per ticker.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient
from kalshi_trader.contract_terms import get_or_fetch_many

_FIELDS = ("rules_primary", "rules_secondary", "subtitle", "yes_sub_title", "no_sub_title")


def _series_ticker(ticker: str) -> str:
    """Reduce a market/event ticker to its series prefix (part before first '-')."""
    return ticker.split("-", 1)[0].upper()


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
    rules_by_ticker = {ticker: rules for ticker, rules in results}

    # Tier 0: merge per-series settlement context. Dedup to distinct series so a
    # strike ladder is one lookup, and the cache makes it free on later cycles.
    series_by_ticker = {ticker: _series_ticker(ticker) for ticker in rules_by_ticker}
    terms_by_series = await get_or_fetch_many(series_by_ticker.values(), client)
    for ticker, rules in rules_by_ticker.items():
        terms = terms_by_series.get(series_by_ticker[ticker])
        if terms:
            rules["settlement_sources"] = terms.get("settlement_sources") or []
            rules["contract_terms_url"] = terms.get("contract_terms_url")

    await client.aclose()
    print(json.dumps(rules_by_ticker, default=str))


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
