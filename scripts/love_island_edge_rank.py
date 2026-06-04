#!/usr/bin/env python3
"""Rank live Love Island markets by edge — the top-25 trade-idea deliverable.

Read-only. Never places orders. For every live (open) teaser-scorable Love Island
contract it computes our signal probability, compares it to the live price, and
ranks the contracts by edge (signal vs price), emitting the top 25.

Signal (deterministic, same components the agent uses):
  - mentions contracts (`KXLOVEISLMENTION*`): the phrase is checked against the
    SAME-DAY teaser transcripts (word-boundary). Spoken → 0.95 (teaser_confirmed);
    otherwise the curated catchphrase prior (prior_only). Per the transcript-gate,
    a transcript must be read first — so these are transcript-backed.
  - binary contracts (bombshell / Casa Amor): the event terms are checked against
    the same-day teaser transcripts. Spoken → 0.95; otherwise no signal (silent).

Edge per contract = best of {buy YES at ask, buy NO at 100-yes_bid} given the
signal probability. Ranked descending; top 25 printed (and dumped to --out).

KEY RULE: only same-day-as-settlement teasers are used (see love_island_backtest).

Usage:
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/love_island_edge_rank.py \
    [--top 25] [--out /tmp/love_island_ideas.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient
from kalshi_trader.external.youtube_client import YouTubeClient
from kalshi_trader.signals.love_island_lexicon import (
    LOVE_ISLAND_CATCHPHRASE_PRIORS,
    lookup_catchphrase_prior,
)
from scripts.love_island_backtest import (
    same_day_window,
    official_channel_for,
    _parse_close,
)

MENTION_SERIES = ["KXLOVEISLMENTION"]
BINARY_SERIES = ["KXLIUSABOMBSHELL", "KXLIUKBOMBSHELL", "KXLIUSACASAAMOR", "KXLIUKCASAAMOR"]
BOMBSHELL_TERMS = ("new bombshell", "bombshell", "new islander", "new boy", "new girl")
CASA_TERMS = ("casa amor", "casa")


def spoken(term: str, transcript: str) -> bool:
    """Word-boundary match so short tokens don't hit inside other words."""
    return re.search(r"\b" + re.escape(term.strip()) + r"\b", transcript) is not None


def best_edge(probability: float, yes_bid: float, yes_ask: float) -> tuple[float, str]:
    """Best edge in cents and the side to take, given the signal probability."""
    buy_yes_edge = probability * 100 - yes_ask
    buy_no_edge = yes_bid - probability * 100
    if buy_yes_edge >= buy_no_edge:
        return round(buy_yes_edge, 1), "YES"
    return round(buy_no_edge, 1), "NO"


def mention_signal(phrase: str, transcript: str) -> tuple[float | None, str]:
    lexicon_hit = lookup_catchphrase_prior(phrase)
    terms = list(LOVE_ISLAND_CATCHPHRASE_PRIORS[lexicon_hit["canonical"]][1]) if lexicon_hit["matched"] else []
    terms += [word for word in re.split(r"[\s/]+", phrase.lower()) if len(word) > 3]
    if any(spoken(term, transcript) for term in terms):
        return 0.95, "teaser_confirmed"
    if lexicon_hit["matched"]:
        return float(lexicon_hit["base_rate"]), "prior_only"
    return None, "no_signal"


def binary_signal(ticker: str, transcript: str) -> tuple[float | None, str]:
    terms = CASA_TERMS if "CASAAMOR" in ticker.upper() else BOMBSHELL_TERMS
    if any(spoken(term, transcript) for term in terms):
        return 0.95, "teaser_confirmed"
    return None, "no_signal"


async def _open_markets(client: KalshiClient, series_list: list[str]) -> list[dict]:
    collected: list[dict] = []
    for series_ticker in series_list:
        cursor = ""
        while True:
            response = await client.get_markets(
                status="open", series_ticker=series_ticker, cursor=cursor, limit=200
            )
            collected += response.get("markets") or []
            cursor = response.get("cursor") or ""
            if not cursor:
                break
    return collected


async def _same_day_transcript(youtube: YouTubeClient, ticker: str, close_datetime) -> str:
    """Concatenated transcripts of the contract's same-day official teasers."""
    published_after, published_before = same_day_window(ticker, close_datetime)
    if not published_after:
        return ""
    videos = await youtube.search_videos(
        "First Look", channel_id=official_channel_for(ticker),
        published_after=published_after, published_before=published_before,
        order="date", max_results=15,
    )
    transcripts = await asyncio.gather(
        *[youtube.fetch_transcript(video["video_id"]) for video in videos]
    )
    return " ".join(transcripts).lower()


async def rank(top: int, out_path: str | None) -> list[dict]:
    youtube = YouTubeClient()
    async with KalshiClient() as client:
        mention_markets = await _open_markets(client, MENTION_SERIES)
        binary_markets = await _open_markets(client, BINARY_SERIES)
    ideas: list[dict] = []
    try:
        # Cache transcripts per (series-region, close) so we fetch each set once.
        transcript_cache: dict[tuple, str] = {}

        async def transcript_for(market: dict) -> str:
            ticker = market.get("ticker", "")
            close_datetime = _parse_close(market)
            after, _ = same_day_window(ticker, close_datetime)
            key = (official_channel_for(ticker), after)
            if key not in transcript_cache:
                transcript_cache[key] = await _same_day_transcript(youtube, ticker, close_datetime)
            return transcript_cache[key]

        for market in mention_markets + binary_markets:
            ticker = market.get("ticker", "")
            yes_bid, yes_ask = market.get("yes_bid"), market.get("yes_ask")
            if yes_bid is None or yes_ask is None:
                continue
            transcript = await transcript_for(market)
            if market in binary_markets:
                probability, evidence = binary_signal(ticker, transcript)
                label = market.get("title", "")
                bucket = "binary_event"
            else:
                label = market.get("yes_sub_title") or ""
                probability, evidence = mention_signal(label, transcript)
                bucket = "mentions"
            if probability is None:
                continue
            edge, side = best_edge(probability, yes_bid, yes_ask)
            ideas.append({
                "ticker": ticker,
                "bucket": bucket,
                "label": label,
                "side": side,
                "probability": round(probability, 3),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "edge_cents": edge,
                "evidence": evidence,
            })
    finally:
        await youtube.close()

    ideas.sort(key=lambda idea: idea["edge_cents"], reverse=True)
    top_ideas = ideas[:top]
    if out_path:
        with open(out_path, "w") as out_file:
            json.dump(top_ideas, out_file, indent=2, default=str)
    _print_table(top_ideas, len(ideas), top)
    return top_ideas


def _print_table(ideas: list[dict], total: int, top: int) -> None:
    print(f"\nTop {min(top, total)} Love Island ideas by edge ({total} scored)\n")
    print(f"{'#':>2} {'EDGE':>5} {'SIDE':>4} {'PROB':>5} {'BID':>4} {'ASK':>4} "
          f"{'EVIDENCE':<16} {'TICKER':<30} LABEL")
    print("-" * 118)
    for rank_index, idea in enumerate(ideas, 1):
        print(f"{rank_index:>2} {idea['edge_cents']:>5} {idea['side']:>4} "
              f"{idea['probability']:>5.2f} {idea['yes_bid']:>4.0f} {idea['yes_ask']:>4.0f} "
              f"{idea['evidence']:<16} {idea['ticker']:<30} {str(idea['label'])[:34]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank live Love Island markets by edge (read-only)")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--out", default="/tmp/love_island_ideas.json")
    args = parser.parse_args()
    asyncio.run(rank(args.top, args.out))


if __name__ == "__main__":
    main()
