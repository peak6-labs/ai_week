#!/usr/bin/env python3
"""Canonical mentions-market scan → one ranked top-25 edge table.

Read-only. Never places orders. Enumerates live "mentions" markets for the given
series, fetches live quotes + the GDELT base rate, scores each via the calibrated
``build_mentions_base_signal``, and writes ``reports/mentions-scan-<TS>.md`` plus a
JSON dump. This is the standard deliverable a mentions run produces — replacing
hand-assembled tables.

Usage:
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/mentions_scan.py \
    [--series KXFEDMENTION ...] [--top-n 25] [--out-dir reports]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient
from kalshi_trader.external.gdelt import GDELTClient
from kalshi_trader.external.mentions_parser import base_rate_from_points, parse_mention_title
from kalshi_trader.mentions_report import DEFAULT_TOP_N, rank_and_render
from kalshi_trader.signals.mentions import build_mentions_base_signal

# Live mentions series seen recently — recurring + event-specific. Extend with --series.
DEFAULT_SERIES = [
    "KXFEDMENTION", "KXBESSENTMTPMENTION", "KXHEARINGMENTION", "KXVANCEMENTION",
    "KXTRUMPMENTION", "KXRUBIOMENTION", "KXNBAMENTION", "KXNHLMENTION",
    "KXMLBMENTION", "KXLOVEISLMENTION",
]
SPORTS_SERIES_PREFIXES = ("KXNBA", "KXNHL", "KXMLB", "KXNFL", "KXUFC", "KXLOVEISL")
# A signal at/above this uncertainty is non-informative (the scorer drops it); we
# mark such rows "suppressed" and give them no tradeable edge.
SUPPRESSED_UNCERTAINTY = 0.99


def _station_for_series(series_ticker: str) -> str:
    return "CNN" if series_ticker.startswith(SPORTS_SERIES_PREFIXES) else "CSPAN"


def _phrase_from_market(market: dict) -> str | None:
    raw_sub_title = (market.get("yes_sub_title") or "").strip()
    if not raw_sub_title or "does not qualify" in raw_sub_title.lower():
        return None
    phrase = raw_sub_title.split("/")[0].strip()
    words = phrase.split()
    if not words or len(words) > 5:
        return None
    return phrase.lower()


def _hours_to_close(close_time_raw: str | None) -> float | None:
    if not close_time_raw:
        return None
    try:
        close_datetime = datetime.fromisoformat(str(close_time_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((close_datetime - datetime.now(timezone.utc)).total_seconds() / 3600, 1)


async def _fetch_open_markets(client: KalshiClient, series_ticker: str) -> list[dict]:
    response = await client.get_markets(status="open", series_ticker=series_ticker, limit=1000)
    return response.get("markets", []) or []


async def run(series_list: list[str], top_n: int, out_dir: str) -> dict:
    scored_rows: list[dict] = []
    gdelt_cache: dict[tuple[str, str], list[dict] | None] = {}

    kalshi_client = KalshiClient()
    gdelt_client = GDELTClient()
    try:
        async with kalshi_client:
            for series_ticker in series_list:
                try:
                    markets = await _fetch_open_markets(kalshi_client, series_ticker)
                except Exception as caught_exception:
                    print(f"  {series_ticker}: fetch error {caught_exception}", file=sys.stderr)
                    continue
                if not markets:
                    continue
                print(f"  {series_ticker}: {len(markets)} open markets", file=sys.stderr)
                station = _station_for_series(series_ticker)
                for market in markets:
                    phrase = _phrase_from_market(market)
                    if phrase is None:
                        continue
                    yes_bid = float(market.get("yes_bid") or 0.0)
                    yes_ask = float(market.get("yes_ask") or 0.0)
                    if yes_ask <= 0.0:
                        continue  # unquoted / no offer
                    title_parse = parse_mention_title(market.get("ticker", ""), market.get("title", "") or "")
                    speaker = (title_parse or {}).get("speaker")

                    cache_key = (phrase, station)
                    if cache_key not in gdelt_cache:
                        try:
                            timeline = await gdelt_client.get_mention_timeline(phrase, stations=station)
                            gdelt_cache[cache_key] = timeline.get("points", []) or []
                            await asyncio.sleep(0.3)
                        except Exception:
                            gdelt_cache[cache_key] = None
                    points = gdelt_cache[cache_key]
                    base_rate = base_rate_from_points(points or [])

                    estimate = build_mentions_base_signal(
                        ticker=market.get("ticker", ""), phrase=phrase, stations=[station],
                        gdelt_base_rate=base_rate, corpus=None, speaker=speaker,
                    )
                    # Suppressed: no signal, or signal flagged non-informative (Phase-2
                    # saturation gate sets uncertainty>=0.99). No tradeable edge.
                    suppressed = estimate is None or float(estimate.uncertainty) >= SUPPRESSED_UNCERTAINTY
                    if suppressed:
                        scored_rows.append({
                            "ticker": market.get("ticker"), "speaker": speaker, "word": phrase,
                            "window": str(market.get("close_time", ""))[:10],
                            "model_probability": (yes_bid + yes_ask) / 200.0, "fair_cents": (yes_bid + yes_ask) / 2.0,
                            "yes_bid": yes_bid, "yes_ask": yes_ask, "side": "—",
                            "edge_cents": 0.0, "volume_24h": market.get("volume_24h", 0),
                            "quality": "suppressed",
                            "hours_to_close": _hours_to_close(market.get("close_time")),
                        })
                        continue

                    probability = float(estimate.probability)
                    fair_cents = probability * 100.0
                    yes_edge = fair_cents - yes_ask
                    no_edge = yes_bid - fair_cents
                    side, edge_cents = ("YES", yes_edge) if yes_edge >= no_edge else ("NO", no_edge)
                    corpus_backed = bool(estimate.metadata.get("independent"))  # corpus-backed tier sets independent=True
                    scored_rows.append({
                        "ticker": market.get("ticker"), "speaker": speaker, "word": phrase,
                        "window": str(market.get("close_time", ""))[:10],
                        "model_probability": probability, "fair_cents": round(fair_cents, 1),
                        "yes_bid": yes_bid, "yes_ask": yes_ask, "side": side,
                        "edge_cents": round(edge_cents, 1), "volume_24h": market.get("volume_24h", 0),
                        "quality": "corpus-backed" if corpus_backed else "gdelt-only",
                        "hours_to_close": _hours_to_close(market.get("close_time")),
                    })
    finally:
        await gdelt_client.close()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    markdown = (
        f"# Mentions scan — {timestamp}\n\n"
        f"{len([r for r in scored_rows if r['quality'] != 'suppressed'])} scored, "
        f"{len([r for r in scored_rows if r['quality'] == 'suppressed'])} suppressed "
        f"(saturated GDELT-only). Live quotes, read-only — no orders.\n\n"
        + rank_and_render(scored_rows, top_n=top_n)
    )
    report_path = Path(out_dir) / f"mentions-scan-{timestamp}.md"
    report_path.write_text(markdown + "\n")
    json_path = Path(out_dir) / f"mentions-scan-{timestamp}.json"
    json_path.write_text(json.dumps(scored_rows, indent=2, default=str))

    print(markdown)
    print(f"\nWrote {report_path} and {json_path}", file=sys.stderr)
    return {"report": str(report_path), "rows": len(scored_rows)}


def _main() -> None:
    parser = argparse.ArgumentParser(description="Scan live mentions markets → ranked edge table (read-only)")
    parser.add_argument("--series", nargs="*", default=DEFAULT_SERIES)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args()
    asyncio.run(run(args.series, args.top_n, args.out_dir))


if __name__ == "__main__":
    _main()
