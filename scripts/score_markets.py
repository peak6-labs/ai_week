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


def _fmt(score_value: float | None) -> str:
    return f"{score_value:.2f}" if score_value is not None else "  -- "


def _coverage(scored_market: ScoredMarket) -> float:
    """Fraction of total weight that actually contributed to the composite."""
    weights = MarketScorer.WEIGHTS
    present = sum(
        weight for signal_name, weight in weights.items()
        if MarketScorer._scores_dict(scored_market).get(signal_name) is not None
    )
    return present / sum(weights.values()) * 100


def _print_debug_block(scored_market: ScoredMarket, rank: int) -> None:
    signal_coverage = _coverage(scored_market)
    print(
        f"\n#{rank:>2}  {scored_market.market.ticker}  score={scored_market.composite_score:.3f}  "
        f"cov={signal_coverage:.0f}%  [{scored_market.market.title[:60]}]"
    )
    print(
        f"     OI%={_fmt(scored_market.volume_oi_ratio_score)}"
        f"  HIST={_fmt(scored_market.relative_historical_volume_score)}"
        f"  SPIKE={_fmt(scored_market.volume_spike_short_term_score)}"
        f"  MOM={_fmt(scored_market.momentum_score)}"
        f"  OI-Δ={_fmt(scored_market.oi_change_score)}"
        f"  IH-HL={_fmt(scored_market.intraday_hl_score)}"
        f"  WK-HL={_fmt(scored_market.weekly_hl_score)}"
        f"  OFI={_fmt(scored_market.ofi_score)}"
        f"  SKEW={_fmt(scored_market.orderbook_skew_score)}"
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

    for scored_market in ranked[:top]:
        print(
            f"{scored_market.market.ticker:<42} "
            f"{scored_market.composite_score:>5.3f}  "
            f"{_fmt(scored_market.volume_oi_ratio_score):>5}  "
            f"{_fmt(scored_market.relative_historical_volume_score):>5}  "
            f"{_fmt(scored_market.volume_spike_short_term_score):>5}  "
            f"{_fmt(scored_market.momentum_score):>5}  "
            f"{_fmt(scored_market.ofi_score):>5}  "
            f"{scored_market.market.title[:55]}"
        )

    print(f"\n{len(ranked)} markets scored total. Top {min(top, len(ranked))} shown.")

    if debug:
        print("\n\n=== DEBUG: full signal breakdown ===")
        for rank_index, scored_market in enumerate(ranked[:top], 1):
            _print_debug_block(scored_market, rank_index)


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
