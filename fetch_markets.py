"""Fetch all live Kalshi markets, apply pipeline filters, and save to a JSON snapshot.

Run once at the start of the day; pass the output to score_markets.py via --markets-file
to skip the slow paginated API fetch on every scoring run.

Usage:
    python fetch_markets.py
    python fetch_markets.py --output markets_2026-06-02.json
    python fetch_markets.py --category economics
    python fetch_markets.py --verbose
"""
import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone

from kalshi_trader.client import KalshiClient
from kalshi_trader.scanner import MarketScanner, filter_markets
from kalshi_trader.market_snapshot import save_snapshot


async def run(output: str, category: str | None) -> None:
    t0 = time.monotonic()

    client = KalshiClient()
    scanner = MarketScanner(client)

    logging.getLogger("kalshi_trader").info("Fetching all open markets...")
    raw = await scanner.get_open_markets()
    logging.getLogger("kalshi_trader").info("Fetched %d raw markets", len(raw))

    now_dt = datetime.now(timezone.utc)
    markets = filter_markets(raw, category, now_dt)

    save_snapshot(markets, output)

    elapsed = time.monotonic() - t0
    print(f"Saved {len(markets)} markets to {output}  ({elapsed:.1f}s)")
    print(f"  Raw from API : {len(raw)}")
    print(f"  After filters: {len(markets)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch and snapshot live Kalshi markets.")
    parser.add_argument("--output", default="live_markets.json", help="Output JSON file path")
    parser.add_argument("--category", default=None, help="Restrict to a single category")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG-level detail")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args.output, args.category))
