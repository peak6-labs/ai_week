"""Score all open Kalshi markets and print the most actionable ones.

Usage:
    python score_markets.py              # top 10 markets
    python score_markets.py --top 25
    python score_markets.py --category sports
    python score_markets.py --verbose    # show DEBUG-level cache detail
"""
import argparse
import asyncio
import logging
from datetime import datetime, timezone

from kalshi_trader.client import KalshiClient
from kalshi_trader.scanner import MarketScanner
from kalshi_trader.actionability import MarketScorer, SnapshotStore


def _fmt(val: float | None) -> str:
    return f"{val:.2f}" if val is not None else "  -- "


async def run(top: int, category: str | None) -> None:
    client  = KalshiClient()
    scanner = MarketScanner(client)
    scorer  = MarketScorer()
    store   = SnapshotStore()

    ranked = await scanner.get_scored_markets(scorer, store, category=category)

    header = (
        f"{'TICKER':<42} {'SCORE':>5}  "
        f"{'OI%':>5}  {'HIST':>5}  {'SPIKE':>5}  "
        f"{'MOM':>5}  {'OFI':>5}  TITLE"
    )
    print(f"\n{header}")
    print("-" * 115)

    for s in ranked[:top]:
        print(
            f"{s.market.ticker:<42} "
            f"{s.composite_score:>5.3f}  "
            f"{_fmt(s.volume_oi_ratio_score):>5}  "
            f"{_fmt(s.relative_historical_volume_score):>5}  "
            f"{_fmt(s.volume_spike_short_term_score):>5}  "
            f"{_fmt(s.momentum_score):>5}  "
            f"{_fmt(s.ofi_score):>5}  "
            f"{s.market.title[:55]}"
        )

    print(f"\n{len(ranked)} markets scored total. Top {min(top, len(ranked))} shown.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score Kalshi markets by actionability.")
    parser.add_argument("--top", type=int, default=10, help="Number of markets to display")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG-level cache detail")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args.top, args.category))
