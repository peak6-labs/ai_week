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
from collections import defaultdict
from datetime import datetime, timezone

from kalshi_trader.actionability import MarketScorer, SnapshotStore
from kalshi_trader.client import KalshiClient
from kalshi_trader.models import ScoredMarket
from kalshi_trader.scanner import MarketScanner


def _fmt(signal_value: float | None) -> str:
    return f"{signal_value:.2f}" if signal_value is not None else "  -- "


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


def _group_by_event(ranked: list[ScoredMarket]) -> list[tuple[float, int, ScoredMarket]]:
    """Group markets by event_ticker, average scores. Returns (avg_score, market_count, best_market)."""
    groups: dict[str, list[ScoredMarket]] = defaultdict(list)
    for scored_market in ranked:
        event_key = scored_market.market.event_ticker or scored_market.market.ticker
        groups[event_key].append(scored_market)
    result = []
    for event_markets in groups.values():
        avg_score = sum(scored_market.composite_score for scored_market in event_markets) / len(event_markets)
        best_market = max(event_markets, key=lambda s: s.composite_score)
        result.append((avg_score, len(event_markets), best_market))
    result.sort(key=lambda x: x[0], reverse=True)
    return result


async def run(top: int, category: str | None, markets_file: str | None, debug: bool) -> None:
    client  = KalshiClient()
    scanner = MarketScanner(client)
    scorer  = MarketScorer()
    store   = SnapshotStore()

    ranked = await scanner.get_scored_markets(
        scorer, store, category=category, markets_file=markets_file
    )

    grouped = _group_by_event(ranked)

    header = (
        f"{'EVENT':<42} {'AVG':>5}  {'N':>2}  "
        f"{'OI%':>5}  {'HIST':>5}  {'SPIKE':>5}  "
        f"{'MOM':>5}  {'OFI':>5}  TITLE"
    )
    print(f"\n{header}")
    print("-" * 120)

    for avg_score, count, best in grouped[:top]:
        print(
            f"{best.market.event_ticker or best.market.ticker:<42} "
            f"{avg_score:>5.3f}  {count:>2}  "
            f"{_fmt(best.volume_oi_ratio_score):>5}  "
            f"{_fmt(best.relative_historical_volume_score):>5}  "
            f"{_fmt(best.volume_spike_short_term_score):>5}  "
            f"{_fmt(best.momentum_score):>5}  "
            f"{_fmt(best.ofi_score):>5}  "
            f"{best.market.title[:50]}"
        )

    unique_events = len(grouped)
    print(f"\n{len(ranked)} markets → {unique_events} events. Top {min(top, unique_events)} shown.")

    if debug:
        print("\n\n=== DEBUG: full signal breakdown (best market per event) ===")
        for rank_index, (_, _, best) in enumerate(grouped[:top], 1):
            _print_debug_block(best, rank_index)


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
