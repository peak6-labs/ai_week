"""Score all open Kalshi markets and print the most actionable ones.

Usage:
    python scripts/score_markets.py              # top 10 markets
    python scripts/score_markets.py --top 25
    python scripts/score_markets.py --category sports
    python scripts/score_markets.py --verbose    # show DEBUG-level cache detail
"""
import argparse
import asyncio
import logging
from datetime import datetime, timezone

from kalshi_trader.actionability import MarketScorer, SnapshotStore
from kalshi_trader.client import KalshiClient
from kalshi_trader.models import ScoredMarket
from kalshi_trader.scanner import MarketScanner


def _fmt(val: float | None) -> str:
    return f"{val:.2f}" if val is not None else "  -- "


def _coverage(s: ScoredMarket) -> float:
    """Fraction of total weight that actually contributed to the composite."""
    weights = MarketScorer.WEIGHTS
    present = sum(
        w for key, w in weights.items()
        if MarketScorer._scores_dict(s).get(key) is not None
    )
    return present / sum(weights.values()) * 100


def _print_debug_block(s: ScoredMarket, rank: int) -> None:
    cov = _coverage(s)
    print(
        f"\n#{rank:>2}  {s.market.ticker}  score={s.composite_score:.3f}  "
        f"cov={cov:.0f}%  [{s.market.title[:60]}]"
    )
    print(
        f"     OI%={_fmt(s.volume_oi_ratio_score)}"
        f"  HIST={_fmt(s.relative_historical_volume_score)}"
        f"  SPIKE={_fmt(s.volume_spike_short_term_score)}"
        f"  MOM={_fmt(s.momentum_score)}"
        f"  OI-Δ={_fmt(s.oi_change_score)}"
        f"  IH-HL={_fmt(s.intraday_hl_score)}"
        f"  WK-HL={_fmt(s.weekly_hl_score)}"
        f"  OFI={_fmt(s.ofi_score)}"
        f"  SKEW={_fmt(s.orderbook_skew_score)}"
    )


async def run(top: int, category: str | None, markets_file: str | None, debug: bool) -> None:
    client  = KalshiClient()
    scanner = MarketScanner(client)
    scorer  = MarketScorer()
    store   = SnapshotStore()

    ranked = await scanner.get_scored_markets(
        scorer, store, category=category, markets_file=markets_file
    )

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

    if debug:
        print("\n\n=== DEBUG: full signal breakdown ===")
        for i, s in enumerate(ranked[:top], 1):
            _print_debug_block(s, i)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score Kalshi markets by actionability.")
    parser.add_argument("--top", type=int, default=10, help="Number of markets to display")
    parser.add_argument("--category", type=str, default=None, help="Filter by category")
    parser.add_argument("--markets-file", type=str, default=None, dest="markets_file",
                        help="Use pre-fetched market snapshot instead of querying the API")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG-level cache detail")
    parser.add_argument("--debug", action="store_true",
                        help="Show all 9 signal scores + weight coverage per market")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args.top, args.category, args.markets_file, args.debug))
