"""Resolve and validate Kalshi website deep links.

Examples:
    PYTHONPATH=. .venv/bin/python scripts/resolve_kalshi_links.py --markets-file live_markets.json --limit 50
    PYTHONPATH=. .venv/bin/python scripts/resolve_kalshi_links.py --event-ticker KXHIGHNY-26MAY29 --series-title "Highest temperature in NYC today?"
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from kalshi_trader.link_resolver import EventInput, fetch_series_title, resolve_event_link
from kalshi_trader.market_snapshot import load_snapshot
from kalshi_trader.web_links import SERIES_SLUGS_PATH, load_series_slugs, save_series_slugs


def run(args: argparse.Namespace) -> None:
    slugs = load_series_slugs(args.cache)
    events = _events_from_args(args)
    resolved = 0

    for idx, event in enumerate(events, start=1):
        if not event.series_ticker:
            print(f"[{idx}] skip {event.event_ticker}: no series ticker")
            continue

        series_key = event.series_ticker.lower()
        series_title = event.series_title
        if not series_title and args.fetch_series_titles:
            try:
                series_title = fetch_series_title(event.series_ticker)
            except Exception as exc:
                print(f"[{idx}] {event.event_ticker}: could not fetch series title ({type(exc).__name__})")

        event = replace(event, series_title=series_title)
        known_slug = slugs.get(series_key)
        result = resolve_event_link(
            event,
            known_slug=known_slug,
            delay_seconds=args.delay,
            strict_title=args.strict_title,
        )
        if not result:
            print(f"[{idx}] unresolved {event.event_ticker} ({event.series_ticker})")
            continue

        slugs[series_key] = result.series_slug
        save_series_slugs(slugs, args.cache)
        resolved += 1
        print(f"[{idx}] resolved {event.event_ticker}: {result.url}")

    print(f"Resolved {resolved}/{len(events)} events. Cache: {args.cache}")


def _events_from_args(args: argparse.Namespace) -> list[EventInput]:
    if args.event_ticker:
        series_ticker = args.series_ticker or args.event_ticker.split("-", 1)[0]
        return [
            EventInput(
                ticker=args.market_ticker or args.event_ticker,
                event_ticker=args.event_ticker,
                series_ticker=series_ticker,
                title=args.title or "",
                series_title=args.series_title or "",
            )
        ]

    markets = load_snapshot(args.markets_file)
    seen: set[str] = set()
    events: list[EventInput] = []
    markets = sorted(markets, key=lambda market: (market.volume_24h, market.open_interest), reverse=True)
    for market in markets:
        series_ticker = market.series_ticker or market.event_ticker.split("-", 1)[0]
        if not series_ticker:
            continue
        if market.event_ticker in seen:
            continue
        seen.add(market.event_ticker)
        events.append(
            EventInput(
                ticker=market.ticker,
                event_ticker=market.event_ticker,
                series_ticker=series_ticker,
                title=market.title,
            )
        )
        if args.limit and len(events) >= args.limit:
            break
    return events


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve verified Kalshi website links.")
    parser.add_argument("--markets-file", default="live_markets.json", help="Market snapshot to resolve from")
    parser.add_argument("--cache", default=str(SERIES_SLUGS_PATH), help="Series slug cache JSON path")
    parser.add_argument("--limit", type=int, default=None, help="Maximum events to try from the snapshot")
    parser.add_argument("--delay", type=float, default=0.15, help="Delay between candidate page fetches")
    parser.add_argument("--strict-title", action="store_true", help="Require page title to overlap the supplied title")
    parser.add_argument("--no-fetch-series-titles", dest="fetch_series_titles", action="store_false")
    parser.set_defaults(fetch_series_titles=True)

    parser.add_argument("--event-ticker", help="Resolve a single event ticker instead of reading the snapshot")
    parser.add_argument("--market-ticker", help="Market ticker for a single-event resolve")
    parser.add_argument("--series-ticker", help="Series ticker for a single-event resolve")
    parser.add_argument("--title", help="Expected event/market title for validation")
    parser.add_argument("--series-title", help="Known series title for candidate slug generation")

    run(parser.parse_args())
