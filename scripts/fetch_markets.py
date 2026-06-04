"""Fetch all live Kalshi markets and save to a JSON snapshot.

Run once at the start of the day; pass the output to score_markets.py via --markets-file
to skip the slow paginated API fetch on every scoring run.

Usage:
    python scripts/fetch_markets.py
    python scripts/fetch_markets.py --output markets_2026-06-02.json
    python scripts/fetch_markets.py --limit 1000   # sample run
    python scripts/fetch_markets.py --verbose
    python scripts/fetch_markets.py --checkpoint-pages 20   # write partial results every 20 pages
    python scripts/fetch_markets.py --page-sleep 0.2        # slow down pagination to avoid 429s
    python scripts/fetch_markets.py --resume                # resume from existing .partial.json
    python scripts/fetch_markets.py --enrich-only           # skip fetch, just enrich categories on existing .partial.json
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import asyncio
import logging
import time
from pathlib import Path

from kalshi_trader.client import KalshiClient
from kalshi_trader.market_snapshot import save_snapshot, save_checkpoint, load_checkpoint, load_snapshot
from kalshi_trader._retry import with_retry
from kalshi_trader.scanner import MarketScanner


async def run(output: str, limit: int | None, checkpoint_pages: int, page_sleep: float, resume: bool, enrich_only: bool) -> None:
    start_time = time.monotonic()
    log = logging.getLogger("kalshi_trader")
    output_path = Path(output)
    checkpoint_path = output_path.with_suffix(".partial.json")

    client = KalshiClient()
    scanner = MarketScanner(client)

    if enrich_only:
        source_path = checkpoint_path if checkpoint_path.exists() else output_path
        if not source_path.exists():
            raise FileNotFoundError(
                f"--enrich-only requires an existing markets file at {checkpoint_path} or {output_path}"
            )
        markets = load_snapshot(source_path, filter_expired=False)
        log.info("--enrich-only: loaded %d markets from %s", len(markets), source_path)
        await scanner.enrich_categories(markets)
        save_snapshot(markets, output)
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        elapsed = time.monotonic() - start_time
        print(f"Saved {len(markets)} markets to {output}  ({elapsed:.1f}s)")
        return

    markets = []
    cursor = ""
    page = 0

    if resume and checkpoint_path.exists():
        markets, cursor, page = load_checkpoint(checkpoint_path)
        log.info("Resuming from checkpoint: %d markets, %d pages completed, cursor=%s…",
                 len(markets), page, cursor[:20] if cursor else "(none)")
    else:
        log.info("Fetching open markets (checkpoint every %d pages → %s)...",
                 checkpoint_pages, checkpoint_path)

    while True:
        response = await with_retry(
            client.get_markets, status="open", cursor=cursor, limit=1000
        )
        page += 1
        batch = response.get("markets", [])
        for market_data in batch:
            markets.append(scanner._parse_market(market_data))
        cursor = response.get("cursor", "")
        log.info("Page %d: %d this page, %d total so far%s",
                 page, len(batch), len(markets), "" if cursor else " — last page")

        if page % checkpoint_pages == 0:
            save_checkpoint(markets, cursor, page, checkpoint_path)
            log.info("Checkpoint written: %d markets, cursor saved → %s", len(markets), checkpoint_path)

        if not cursor:
            break
        if limit is not None and len(markets) >= limit:
            markets = markets[:limit]
            log.info("Reached --limit %d, stopping early", limit)
            break

        if page_sleep > 0:
            await asyncio.sleep(page_sleep)

    log.info("Fetched %d markets", len(markets))
    await scanner.enrich_categories(markets)

    save_snapshot(markets, output)

    # Clean up checkpoint now that the full file is written
    if checkpoint_path.exists():
        checkpoint_path.unlink()

    elapsed = time.monotonic() - start_time
    print(f"Saved {len(markets)} markets to {output}  ({elapsed:.1f}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch and snapshot live Kalshi markets.")
    parser.add_argument("--output", default="live_markets.json", help="Output JSON file path")
    parser.add_argument("--limit", type=int, default=None, help="Stop after fetching this many markets (for testing)")
    parser.add_argument("--checkpoint-pages", type=int, default=20, help="Write partial results to .partial.json every N pages (default: 20)")
    parser.add_argument("--page-sleep", type=float, default=0.1, help="Seconds to sleep between pages to avoid rate limiting (default: 0.1)")
    parser.add_argument("--resume", action="store_true", help="Resume from existing .partial.json checkpoint")
    parser.add_argument("--enrich-only", action="store_true", help="Skip fetch, run only category enrichment on existing .partial.json or output file")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG-level detail")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args.output, args.limit, args.checkpoint_pages, args.page_sleep, args.resume, args.enrich_only))
