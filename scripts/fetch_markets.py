"""Fetch all live Kalshi markets and save to a JSON snapshot.

Run once at the start of the day; pass the output to score_markets.py via --markets-file
to skip the slow paginated API fetch on every scoring run.

Usage:
    python scripts/fetch_markets.py
    python scripts/fetch_markets.py --output markets_2026-06-02.json
    python scripts/fetch_markets.py --limit 1000   # sample run
    python scripts/fetch_markets.py --verbose
"""
import argparse
import asyncio
import logging
import time

from kalshi_trader.client import KalshiClient
from kalshi_trader.scanner import MarketScanner
from kalshi_trader.market_snapshot import save_snapshot


async def run(output: str, limit: int | None) -> None:
    t0 = time.monotonic()

    client = KalshiClient()
    scanner = MarketScanner(client)

    logging.getLogger("kalshi_trader").info("Fetching open markets...")
    markets = await scanner.get_open_markets(limit=limit)
    logging.getLogger("kalshi_trader").info("Fetched %d markets", len(markets))

    await scanner.enrich_categories(markets)

    save_snapshot(markets, output)

    elapsed = time.monotonic() - t0
    print(f"Saved {len(markets)} markets to {output}  ({elapsed:.1f}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch and snapshot live Kalshi markets.")
    parser.add_argument("--output", default="live_markets.json", help="Output JSON file path")
    parser.add_argument("--limit", type=int, default=None, help="Stop after fetching this many markets (for testing)")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG-level detail")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args.output, args.limit))
