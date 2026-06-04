#!/usr/bin/env python3
"""Backtest the Love Island teaser signal against settled Kalshi markets.

Read-only. Never places orders. Tests one falsifiable claim: does an official
pre-episode YouTube teaser ("First Look") predict whether a teaser-driven event
(a new bombshell, Casa Amor) actually happened that episode?

Ground truth is Kalshi's own **settled** markets — not a hand-curated table — so
nothing here is fabricated. For each settled, teaser-scorable Love Island market we:

  1. read its realized outcome (``result`` is "yes"/"no") and its close time;
  2. search YouTube for the official teaser published on the **same calendar day the
     market settles** (the episode date in the ticker) — the KEY RULE — capped at the
     close time for no look-ahead, so we never match a past episode/season/spin-off;
  3. keyword-match the teaser title+description against the event's terms to get a
     predicted probability (run through ``build_love_island_signal`` so the real
     builder is exercised);
  4. compare predicted probability to the realized 0/1 outcome.

Outputs Brier score + hit-rate (overall and per bucket), a naive base-rate
baseline, and a JSON dump.

Caveats (honest, by design):
  - Grok X sentiment is NOT backtestable (x_search is real-time-only), so this
    isolates the YouTube-teaser component. The live agent layers X on top.
  - Signal = teaser title+description (Peacock caption text is not API-fetchable),
    so only buckets a teaser plausibly reveals (bombshell, Casa Amor) are scored;
    winner/couple/rank markets are public-vote driven and skipped here.

Usage:
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/love_island_backtest.py --smoke
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/love_island_backtest.py \
    --max-markets 8 --out /tmp/love_island_backtest.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env so API keys are set
from kalshi_trader.client import KalshiClient
from kalshi_trader.external.youtube_client import (
    LOVE_ISLAND_UK_CHANNEL_ID,
    LOVE_ISLAND_USA_CHANNEL_ID,
    YouTubeClient,
)
from kalshi_trader.signals.love_island import build_love_island_signal

# Love Island series prefixes to sweep for settled markets.
LOVE_ISLAND_SERIES = [
    "KXLIUSABOMBSHELL",
    "KXLIUKBOMBSHELL",
    "KXLIUSACASAAMOR",
    "KXLIUKCASAAMOR",
]

# Teaser-event classification: ticker substring → (bucket, match terms a teaser
# would use to reveal the event). Only teaser-revealable events are scored.
EVENT_TERMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "BOMBSHELL": ("binary_event", ("bombshell", "new islander", "new boy",
                                    "new girl", "arrive", "enters the villa", "enter the villa")),
    "CASAAMOR": ("binary_event", ("casa amor", "casa")),
}

CALIBRATION_BINS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001)]


def classify_market(ticker: str) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(bucket, match_terms)`` for a teaser-scorable market, else None."""
    upper_ticker = (ticker or "").upper()
    for event_key, classification in EVENT_TERMS.items():
        if event_key in upper_ticker:
            return classification
    return None


_MONTH_ABBREVS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")


def ticker_episode_date(ticker: str) -> datetime | None:
    """Episode/settlement date encoded in the ticker (e.g. ...-26JUN05 → 2026-06-05).

    The KEY RULE anchors teasers to the show's episode day, NOT the UTC close
    timestamp (which rolls to the next day for US-evening episodes and would skip the
    real same-day teaser). Returns midnight UTC of that date, or None if unparseable.
    """
    match = _TICKER_DATE.search((ticker or "").upper())
    if not match:
        return None
    year_suffix, month_abbrev, day = match.groups()
    month = _MONTH_ABBREVS.get(month_abbrev)
    if not month:
        return None
    return datetime(2000 + int(year_suffix), month, int(day), tzinfo=timezone.utc)


def same_day_window(ticker: str, close_datetime: datetime | None) -> tuple[str | None, str | None]:
    """RFC-3339 (published_after, published_before) bounding teasers to the SAME
    calendar day the market settles — the episode date in the ticker.

    Upper bound is capped at the actual close so a teaser published after settlement
    is never used (no look-ahead). Falls back to the close date when the ticker has
    no parseable date.
    """
    episode_date = ticker_episode_date(ticker)
    if episode_date is None and close_datetime is not None:
        episode_date = close_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
    if episode_date is None:
        return None, None
    day_start = episode_date
    day_end = episode_date + timedelta(days=1)
    upper_bound = min(day_end, close_datetime) if close_datetime is not None else day_end
    return (
        day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        upper_bound.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def teaser_query_for(ticker: str) -> str:
    """Build the YouTube search query for the episode's official teaser.

    Deliberately carries NO date: YouTube search treats a date in the query as
    plain keywords, not a filter — the real date control is the published_after /
    published_before window passed to ``search_videos``.
    """
    region = "UK" if "LIUK" in (ticker or "").upper() else "USA"
    return f"Love Island {region} First Look"


def official_channel_for(ticker: str) -> str:
    """Official channel id for the ticker's region (scopes out fan re-uploads)."""
    return LOVE_ISLAND_UK_CHANNEL_ID if "LIUK" in (ticker or "").upper() else LOVE_ISLAND_USA_CHANNEL_ID


def predicted_probability(
    teaser_text: str, match_terms: tuple[str, ...], confirm_prob: float, silent_prob: float
) -> tuple[float, str, bool]:
    """Teaser title+description → (probability, evidence_strength, matched)."""
    normalized = teaser_text.lower()
    matched = any(term in normalized for term in match_terms)
    if matched:
        return confirm_prob, "teaser_confirmed", True
    # Teaser silent: absence is weak evidence, not proof. Lean to silent_prob.
    return silent_prob, "sentiment_only", False


def _brier(samples: list[dict]) -> float:
    if not samples:
        return float("nan")
    return sum((sample["probability"] - sample["realized"]) ** 2 for sample in samples) / len(samples)


def _hit_rate(samples: list[dict]) -> float:
    """Fraction where a >0.5 prediction matched the outcome (ties excluded)."""
    decisive = [sample for sample in samples if sample["probability"] != 0.5]
    if not decisive:
        return float("nan")
    correct = sum(
        1 for sample in decisive
        if (sample["probability"] > 0.5) == (sample["realized"] == 1)
    )
    return correct / len(decisive)


def _calibration_table(samples: list[dict]) -> list[dict]:
    table = []
    for bin_low, bin_high in CALIBRATION_BINS:
        bucket = [sample for sample in samples if bin_low <= sample["probability"] < bin_high]
        if not bucket:
            table.append({"bin": f"[{bin_low:.2f},{bin_high:.2f})", "count": 0,
                          "mean_predicted": None, "actual_yes_rate": None})
            continue
        mean_predicted = sum(sample["probability"] for sample in bucket) / len(bucket)
        actual_yes_rate = sum(sample["realized"] for sample in bucket) / len(bucket)
        table.append({
            "bin": f"[{bin_low:.2f},{bin_high:.2f})",
            "count": len(bucket),
            "mean_predicted": round(mean_predicted, 4),
            "actual_yes_rate": round(actual_yes_rate, 4),
        })
    return table


async def _settled_love_island_markets(client: KalshiClient, max_markets: int) -> list[dict]:
    """Read settled, teaser-scorable Love Island markets (with realized results)."""
    collected: list[dict] = []
    for series_ticker in LOVE_ISLAND_SERIES:
        cursor = ""
        while True:
            response = await client.get_markets(
                status="settled", series_ticker=series_ticker, cursor=cursor, limit=200
            )
            for market in response.get("markets") or []:
                result = (market.get("result") or "").lower()
                if result not in ("yes", "no"):
                    continue
                if classify_market(market.get("ticker", "")) is None:
                    continue
                collected.append(market)
            cursor = response.get("cursor") or ""
            if not cursor or len(collected) >= max_markets:
                break
        if len(collected) >= max_markets:
            break
    return collected[:max_markets]


async def run_smoke(youtube: YouTubeClient) -> None:
    """Pull one prior teaser and print it — validates the YouTube path end-to-end."""
    query = "First Look"
    records = await youtube.search_videos(
        query, channel_id=LOVE_ISLAND_USA_CHANNEL_ID, order="date", max_results=5
    )
    if not records:
        print("SMOKE: no results (is YOUTUBE_API_KEY set and valid?)", file=sys.stderr)
        return
    print(f"SMOKE: '{query}' → {len(records)} videos")
    for record in records:
        print(f"  [{record['published_at']}] {record['title']}  (id={record['video_id']})")


async def run_backtest(
    youtube: YouTubeClient, max_markets: int, confirm_prob: float, silent_prob: float, out_path: str
) -> dict:
    async with KalshiClient() as client:
        markets = await _settled_love_island_markets(client, max_markets)

    samples: list[dict] = []
    for market in markets:
        ticker = market.get("ticker", "")
        classification = classify_market(ticker)
        if classification is None:
            continue
        bucket, match_terms = classification
        realized = 1 if (market.get("result") or "").lower() == "yes" else 0

        close_datetime = _parse_close(market)
        published_after, published_before = same_day_window(ticker, close_datetime)
        query = teaser_query_for(ticker)

        # KEY RULE: only same-day-as-settlement teasers (order=date), so we never
        # match a teaser from a past episode/season/spin-off.
        teaser_records = await youtube.search_videos(
            query,
            channel_id=official_channel_for(ticker),
            published_before=published_before,
            published_after=published_after,
            order="date",
            max_results=10,
        )
        # Pull each in-window teaser's transcript (best-effort) so the match reads
        # what the teaser SAYS, not just its title/description.
        transcripts = await asyncio.gather(
            *[youtube.fetch_transcript(record["video_id"]) for record in teaser_records]
        )
        transcript_chars = sum(len(text) for text in transcripts)
        teaser_text = " ".join(
            f"{record['title']} {record['description']} {transcript}"
            for record, transcript in zip(teaser_records, transcripts)
        )
        probability, evidence_strength, matched = predicted_probability(
            teaser_text, match_terms, confirm_prob, silent_prob
        )
        # Exercise the real builder (validates clamping + metadata shape).
        estimate = build_love_island_signal(
            ticker=ticker,
            probability=probability,
            evidence_strength=evidence_strength,
            market_bucket=bucket,
            narrative=f"Backtest teaser match={matched} for {query!r}",
            sources=[f"youtube:{record['video_id']}" for record in teaser_records[:3]],
        )
        samples.append({
            "ticker": ticker,
            "bucket": bucket,
            "probability": estimate.probability,
            "realized": realized,
            "matched": matched,
            "teaser_count": len(teaser_records),
            "transcript_chars": transcript_chars,
            "query": query,
        })

    base_yes_rate = (sum(sample["realized"] for sample in samples) / len(samples)) if samples else float("nan")
    naive_samples = [{**sample, "probability": base_yes_rate} for sample in samples]
    results = {
        "n": len(samples),
        "base_yes_rate": round(base_yes_rate, 4) if samples else None,
        "brier": round(_brier(samples), 4) if samples else None,
        "naive_brier": round(_brier(naive_samples), 4) if samples else None,
        "hit_rate": round(_hit_rate(samples), 4) if samples else None,
        "calibration": _calibration_table(samples),
        "samples": samples,
    }
    with open(out_path, "w") as out_file:
        json.dump(results, out_file, indent=2, default=str)
    _print_report(results, out_path)
    return results


def _parse_close(market: dict) -> datetime | None:
    """Best-effort close datetime (UTC) from a settled market."""
    raw = market.get("close_time")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _print_report(results: dict, out_path: str) -> None:
    print(f"\nLove Island teaser backtest — n={results['n']} "
          f"(base YES rate={results['base_yes_rate']})")
    if not results["n"]:
        print("  no settled, teaser-scorable markets found (need KALSHI_ENV=prod).")
        return
    print(f"  teaser model Brier={results['brier']}  (naive base-rate Brier={results['naive_brier']})")
    print(f"  hit-rate={results['hit_rate']}")
    print("  calibration (predicted bin → actual YES rate):")
    for row in results["calibration"]:
        if row["count"]:
            print(f"    {row['bin']}  n={row['count']:>3}  "
                  f"predicted={row['mean_predicted']}  actual={row['actual_yes_rate']}")
    print(f"  full dump → {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Love Island teaser signal backtest (read-only)")
    parser.add_argument("--smoke", action="store_true", help="Pull one teaser and print it, then exit.")
    parser.add_argument("--max-markets", type=int, default=8)
    parser.add_argument("--confirm-prob", type=float, default=0.85,
                        help="Predicted P(YES) when the teaser confirms the event.")
    parser.add_argument("--silent-prob", type=float, default=0.40,
                        help="Predicted P(YES) when the teaser is silent on the event.")
    parser.add_argument("--out", default="/tmp/love_island_backtest.json")
    args = parser.parse_args()

    async def run() -> None:
        youtube = YouTubeClient()
        try:
            if args.smoke:
                await run_smoke(youtube)
            else:
                await run_backtest(
                    youtube, args.max_markets, args.confirm_prob, args.silent_prob, args.out
                )
        finally:
            await youtube.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
