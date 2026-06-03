#!/usr/bin/env python
"""Re-price open paper recommendations to the live market.

Older recommendations recorded their entry from a stale snapshot, so the entry
prices shown in Ideas History no longer match the live market. This refreshes
each OPEN recommendation's ``entry_price_cents`` (and recomputes ``edge_cents``)
from the live top-of-book, on the side that was recorded.

Read-only by default (prints what would change). Pass --apply to write to
Supabase. Resolved recommendations and tickers with no live market are skipped.

    KALSHI_ENV=prod PYTHONPATH=. python scripts/reprice_recommendations.py            # dry run
    KALSHI_ENV=prod PYTHONPATH=. python scripts/reprice_recommendations.py --apply
"""
from __future__ import annotations

import argparse
import asyncio

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader import db
from kalshi_trader.actionability.signals import live_top_of_book
from kalshi_trader.client import KalshiClient


async def _live_prices(tickers: list[str]) -> dict[str, tuple[float | None, float | None]]:
    """Fetch live (yes_bid, yes_ask) per ticker, concurrency-bounded."""
    concurrency_semaphore = asyncio.Semaphore(8)
    prices: dict[str, tuple[float | None, float | None]] = {}

    async def _one(client: KalshiClient, ticker: str) -> None:
        async with concurrency_semaphore:
            try:
                orderbook_response = await client.get_orderbook(ticker)
                yes_bid, yes_ask = live_top_of_book(orderbook_response.get("orderbook") or {})
                if yes_bid is None or yes_ask is None:
                    market = (await client.get_market(ticker)).get("market", {})
                    yes_bid = market.get("yes_bid") if yes_bid is None else yes_bid
                    yes_ask = market.get("yes_ask") if yes_ask is None else yes_ask
                prices[ticker] = (yes_bid, yes_ask)
            except Exception as caught_exception:
                prices[ticker] = (None, None)
                print(f"  ! {ticker}: live fetch failed ({str(caught_exception)[:60]})")

    async with KalshiClient() as client:
        await asyncio.gather(*[_one(client, ticker) for ticker in tickers])
    return prices


def _entry_for_side(side: str, yes_bid: float | None, yes_ask: float | None) -> float | None:
    """Taker entry cost on the recorded side: buy YES at ask, buy NO at 100-bid."""
    if side == "yes":
        return yes_ask
    if yes_bid is None:
        return None
    return 100.0 - yes_bid


async def main(apply_changes: bool) -> None:
    recommendations = await db.fetch_open_recommendations()
    print(f"open recommendations: {len(recommendations)}")
    tickers = sorted({rec.get("ticker", "") for rec in recommendations if rec.get("ticker")})
    prices = await _live_prices(tickers)

    client = await db._get_client()
    repriced = 0
    skipped = 0
    for recommendation in recommendations:
        ticker = recommendation.get("ticker", "")
        side = recommendation.get("side", "yes")
        old_entry = float(recommendation.get("entry_price_cents") or 0)
        yes_bid, yes_ask = prices.get(ticker, (None, None))
        new_entry = _entry_for_side(side, yes_bid, yes_ask)
        if new_entry is None or new_entry <= 0 or new_entry >= 100:
            skipped += 1
            print(f"  skip {ticker} ({side}): no live price")
            continue
        new_entry = round(new_entry, 2)
        predicted_probability = float(recommendation.get("predicted_prob") or 0)
        new_edge = round(predicted_probability * 100 - new_entry, 2)
        if abs(new_entry - old_entry) < 0.01:
            continue
        print(f"  {ticker:<44} {side:<3} entry {old_entry:>5.1f} -> {new_entry:>5.1f}  edge -> {new_edge:>5.1f}")
        repriced += 1
        if apply_changes:
            await (
                client.table("recommendations")
                .update({"entry_price_cents": new_entry, "edge_cents": new_edge})
                .eq("id", recommendation.get("rec_id"))
                .execute()
            )

    action = "REPRICED" if apply_changes else "would reprice"
    print(f"\n{action} {repriced}, skipped {skipped} (no live market), of {len(recommendations)} open.")
    if not apply_changes:
        print("Dry run — re-run with --apply to write to Supabase.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-price open recommendations to live market")
    parser.add_argument("--apply", action="store_true", help="Write updates to Supabase (default: dry run)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
