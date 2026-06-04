#!/usr/bin/env python3
"""Backtest the mentions signal's calibration against settled Kalshi markets.

Read-only. Never places orders. Measures whether the GDELT TV base rate (the
``mentions_base`` signal in its GDELT-only mode) actually predicts whether a
phrase gets said in a single event — the premise the trace audit questioned.

For each settled mentions market we:
  1. read its realized outcome (``result`` is "yes"/"no") and phrase
     (``yes_sub_title``);
  2. reconstruct the GDELT base rate **as of the market's close** (points after
     the close month are dropped to avoid look-ahead);
  3. produce the model probability via ``build_mentions_base_signal``;
  4. compare predicted probability to the realized 0/1 outcome.

Outputs a calibration report (Brier score, hit-rate, a probability-binned
calibration table, and a GDELT-only vs corpus-backed split) plus a JSON dump.

Usage:
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/mentions_backtest.py \
    --out /tmp/mentions_backtest.json [--max-per-series 200] [--series KXNBAMENTION ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

import numpy

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env
from kalshi_trader.client import KalshiClient
from kalshi_trader.external.gdelt import GDELTClient
from kalshi_trader.external.mentions_parser import base_rate_from_points, parse_mention_title
from kalshi_trader.external.speaker_registry import VENUE_FED_PRESSER
from kalshi_trader.mentions.store import MentionsArchiveStore
from kalshi_trader.signals.mentions import (
    CORPUS_BACKED_DOC_THRESHOLD,
    build_mentions_base_signal,
)

# Series whose speaker is the FOMC Chair (Powell); the 2B Fed-corpus tie-in counts
# his presser transcripts as-of each market's close.
FED_SERIES = "KXFEDMENTION"
FOMC_CHAIR_KEY = "powell"

# Recurring mentions series that settle frequently — the bulk of the sample.
# Sports/entertainment name-mentions resolve per game/episode; the Fed/political
# ones resolve per meeting. Extend with --series.
DEFAULT_SERIES = [
    "KXNBAMENTION",
    "KXNHLMENTION",
    "KXMLBMENTION",
    "KXFEDMENTION",
    "KXLOVEISLMENTION",
]
# Series whose venue is a sports/entertainment broadcast — queried on national
# news (CNN) as the GDELT proxy, mirroring extract_phrase_from_settlement.
SPORTS_SERIES_PREFIXES = ("KXNBA", "KXNHL", "KXMLB", "KXNFL", "KXUFC", "KXLOVEISL")

# Probability bins for the calibration table.
CALIBRATION_BINS = [(0.0, 0.15), (0.15, 0.50), (0.50, 0.85), (0.85, 1.01)]


def _close_year_month(close_time_raw: str) -> str:
    """Return 'YYYYMM' for a market's close timestamp (for look-ahead truncation)."""
    digits = "".join(character for character in (close_time_raw or "") if character.isdigit())
    return digits[:6] if len(digits) >= 6 else "999912"


def _point_year_month(point_date_raw: str) -> str:
    """Return 'YYYYMM' for a GDELT timeline point date string."""
    digits = "".join(character for character in (point_date_raw or "") if character.isdigit())
    return digits[:6] if len(digits) >= 6 else "000000"


def close_date_iso(close_time_raw: str) -> str:
    """Return 'YYYY-MM-DD' for a market's close timestamp (the as-of corpus cutoff).

    Day precision (vs ``_close_year_month``'s month) so the Fed-corpus count excludes
    pressers held on or after the day the market closes — the ``until`` bound is strict.
    """
    digits = "".join(character for character in (close_time_raw or "") if character.isdigit())
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return "9999-12-31"


def _station_for_series(series_ticker: str) -> str:
    """CNN for sports/entertainment broadcast venues, CSPAN for gov venues."""
    return "CNN" if series_ticker.startswith(SPORTS_SERIES_PREFIXES) else "CSPAN"


def _phrase_from_market(market: dict) -> str | None:
    """The tracked phrase — yes_sub_title is the canonical keyword field."""
    raw_sub_title = (market.get("yes_sub_title") or "").strip()
    if not raw_sub_title or "does not qualify" in raw_sub_title.lower():
        return None
    phrase = raw_sub_title.split("/")[0].strip()  # "China / Chinese" -> "China"
    words = phrase.split()
    if not words or len(words) > 5:
        return None
    return phrase.lower()


async def _fetch_settled_markets(
    client: KalshiClient, series_ticker: str, max_per_series: int
) -> list[dict]:
    """Paginate settled markets for one series (read-only)."""
    collected: list[dict] = []
    cursor = ""
    while len(collected) < max_per_series:
        response = await client.get_markets(
            status="settled", series_ticker=series_ticker, limit=1000, cursor=cursor
        )
        markets = response.get("markets", []) or []
        collected.extend(markets)
        cursor = response.get("cursor", "") or ""
        if not cursor or not markets:
            break
    return collected[:max_per_series]


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
        bucket = [s for s in samples if bin_low <= s["probability"] < bin_high]
        if not bucket:
            table.append({"bin": f"[{bin_low:.2f},{bin_high:.2f})", "count": 0,
                          "mean_predicted": None, "actual_yes_rate": None})
            continue
        mean_predicted = sum(s["probability"] for s in bucket) / len(bucket)
        actual_yes_rate = sum(s["realized"] for s in bucket) / len(bucket)
        table.append({
            "bin": f"[{bin_low:.2f},{bin_high:.2f})",
            "count": len(bucket),
            "mean_predicted": round(mean_predicted, 4),
            "actual_yes_rate": round(actual_yes_rate, 4),
        })
    return table


async def run(
    series_list: list[str],
    max_per_series: int,
    out_path: str,
    *,
    fed_corpus_asof: bool = False,
    db_path: str | None = None,
) -> dict:
    samples: list[dict] = []
    skipped = {"no_phrase": 0, "no_result": 0, "no_signal": 0, "gdelt_error": 0}
    gdelt_cache: dict[tuple[str, str], list[dict]] = {}

    # 2B Fed-corpus tie-in: count Powell's presser transcripts as-of each Fed market's
    # close from a populated (dedicated) archive. Off by default → pure GDELT regime.
    archive_store = None
    if fed_corpus_asof:
        if db_path == MentionsArchiveStore.DEFAULT_DB_PATH:
            raise SystemExit("refusing to read the production archive; pass a dedicated --db")
        archive_store = MentionsArchiveStore(db_path=db_path or "/tmp/mentions_premise_archive.db")

    kalshi_client = KalshiClient()
    gdelt_client = GDELTClient()
    try:
        async with kalshi_client:
            for series_ticker in series_list:
                try:
                    markets = await _fetch_settled_markets(kalshi_client, series_ticker, max_per_series)
                except Exception as caught_exception:
                    print(f"  {series_ticker}: fetch error {caught_exception}", file=sys.stderr)
                    continue
                print(f"  {series_ticker}: {len(markets)} settled markets", file=sys.stderr)
                for market in markets:
                    result = (market.get("result") or "").lower()
                    if result not in ("yes", "no"):
                        skipped["no_result"] += 1
                        continue
                    phrase = _phrase_from_market(market)
                    if phrase is None:
                        skipped["no_phrase"] += 1
                        continue
                    station = _station_for_series(series_ticker)
                    title_parse = parse_mention_title(market.get("ticker", ""), market.get("title", "") or "")
                    speaker = (title_parse or {}).get("speaker")

                    cache_key = (phrase, station)
                    if cache_key not in gdelt_cache:
                        try:
                            timeline = await gdelt_client.get_mention_timeline(phrase, stations=station)
                            gdelt_cache[cache_key] = timeline.get("points", []) or []
                            await asyncio.sleep(0.3)  # polite spacing; GDELT rate-limits
                        except Exception as caught_exception:
                            gdelt_cache[cache_key] = None  # mark errored
                            print(f"    GDELT error '{phrase}'/{station}: {caught_exception}", file=sys.stderr)
                    points = gdelt_cache[cache_key]
                    if points is None:
                        skipped["gdelt_error"] += 1
                        continue

                    # Look-ahead guard: keep only periods at/before the close month.
                    close_year_month = _close_year_month(market.get("close_time", ""))
                    points_asof = [p for p in points if _point_year_month(p.get("date", "")) <= close_year_month]
                    base_rate = base_rate_from_points(points_asof)

                    # 2B: for Fed markets, count Powell's pressers strictly before the
                    # market close (no look-ahead). Otherwise stay in the GDELT regime.
                    corpus = None
                    speaker_key = None
                    if archive_store is not None and series_ticker == FED_SERIES:
                        speaker_key = FOMC_CHAIR_KEY
                        corpus = archive_store.count_phrase(
                            speaker_key=speaker_key,
                            venue_type=VENUE_FED_PRESSER,
                            phrase=phrase,
                            until=close_date_iso(market.get("close_time", "")),
                        )
                    estimate = build_mentions_base_signal(
                        ticker=market.get("ticker", ""),
                        phrase=phrase,
                        stations=[station],
                        gdelt_base_rate=base_rate,
                        corpus=corpus,
                        speaker=speaker,
                        speaker_key=speaker_key,
                    )
                    if estimate is None:
                        skipped["no_signal"] += 1
                        continue
                    document_count = int(estimate.metadata.get("corpus_document_count", 0) or 0)
                    samples.append({
                        "ticker": market.get("ticker"),
                        "series": series_ticker,
                        "phrase": phrase,
                        "station": station,
                        "probability": round(float(estimate.probability), 4),
                        "realized": 1 if result == "yes" else 0,
                        "period_count": base_rate["period_count"],
                        "p_gdelt": round(float(estimate.metadata.get("p_gdelt", 0.0)), 4),
                        "corpus_backed": document_count >= CORPUS_BACKED_DOC_THRESHOLD,
                        "close_time": market.get("close_time"),
                        "volume": market.get("volume", 0),
                    })
    finally:
        await gdelt_client.close()
        if archive_store is not None:
            archive_store.close()

    gdelt_only = [s for s in samples if not s["corpus_backed"]]
    corpus_backed = [s for s in samples if s["corpus_backed"]]
    base_yes_rate = (sum(s["realized"] for s in samples) / len(samples)) if samples else float("nan")
    # Naive baseline: always predict the overall yes base rate.
    naive_brier = (sum((base_yes_rate - s["realized"]) ** 2 for s in samples) / len(samples)) if samples else float("nan")
    fed_corpus_report = _fed_corpus_report(samples) if fed_corpus_asof else None

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "series": series_list,
        "n_samples": len(samples),
        "skipped": skipped,
        "base_yes_rate": round(base_yes_rate, 4) if samples else None,
        "overall": {
            "brier": round(_brier(samples), 4) if samples else None,
            "naive_brier": round(naive_brier, 4) if samples else None,
            "hit_rate": round(_hit_rate(samples), 4) if samples else None,
            "calibration": _calibration_table(samples),
        },
        "gdelt_only": {
            "n": len(gdelt_only),
            "brier": round(_brier(gdelt_only), 4) if gdelt_only else None,
            "hit_rate": round(_hit_rate(gdelt_only), 4) if gdelt_only else None,
            "calibration": _calibration_table(gdelt_only),
        },
        "corpus_backed": {
            "n": len(corpus_backed),
            "brier": round(_brier(corpus_backed), 4) if corpus_backed else None,
            "hit_rate": round(_hit_rate(corpus_backed), 4) if corpus_backed else None,
            "calibration": _calibration_table(corpus_backed),
        },
        "fed_corpus_report": fed_corpus_report,
        "samples": samples,
    }
    with open(out_path, "w") as handle:
        json.dump(results, handle, indent=2, default=str)

    _print_summary(results)
    return results


def _bootstrap_brier_interval(samples: list[dict], draws: int = 2000, seed: int = 12345) -> dict:
    """Bootstrap 2.5/50/97.5 percentiles of a sample set's Brier (small-n honesty)."""
    squared_errors = numpy.array(
        [(sample["probability"] - sample["realized"]) ** 2 for sample in samples], dtype=float
    )
    random_generator = numpy.random.default_rng(seed)
    sample_count = len(squared_errors)
    bootstrap_briers = numpy.array([
        squared_errors[random_generator.integers(0, sample_count, sample_count)].mean()
        for _ in range(draws)
    ])
    return {
        "percentile_2_5": round(float(numpy.percentile(bootstrap_briers, 2.5)), 4),
        "percentile_50": round(float(numpy.percentile(bootstrap_briers, 50)), 4),
        "percentile_97_5": round(float(numpy.percentile(bootstrap_briers, 97.5)), 4),
    }


def _fed_corpus_report(samples: list[dict]) -> dict:
    """2B sub-report: corpus-backed vs GDELT-only vs naive on KXFEDMENTION only.

    Small n (≈48, far fewer once corpus-backing requires ≥12 pressers), so the
    corpus-backed Brier carries a bootstrap credible interval and a directional-only
    caveat rather than a bare point estimate.
    """
    fed_samples = [sample for sample in samples if sample["series"] == FED_SERIES]
    if not fed_samples:
        return {"n_fed": 0, "note": "no KXFEDMENTION samples in this run"}
    corpus_backed = [sample for sample in fed_samples if sample["corpus_backed"]]
    gdelt_only = [sample for sample in fed_samples if not sample["corpus_backed"]]
    fed_yes_rate = sum(sample["realized"] for sample in fed_samples) / len(fed_samples)
    naive_samples = [
        {"probability": fed_yes_rate, "realized": sample["realized"]} for sample in fed_samples
    ]
    report = {
        "n_fed": len(fed_samples),
        "n_corpus_backed": len(corpus_backed),
        "n_gdelt_only": len(gdelt_only),
        "fed_yes_rate": round(fed_yes_rate, 4),
        "naive_brier": round(_brier(naive_samples), 4),
        "gdelt_only_brier": round(_brier(gdelt_only), 4) if gdelt_only else None,
        "corpus_backed_brier": round(_brier(corpus_backed), 4) if corpus_backed else None,
        "corpus_backed_brier_credible_interval": (
            _bootstrap_brier_interval(corpus_backed) if len(corpus_backed) >= 2 else None
        ),
    }
    if len(corpus_backed) < 15:
        report["caveat"] = (
            f"only {len(corpus_backed)} corpus-backed Fed samples — directional only, "
            f"not significant; if 0, the archive lacked enough pre-close pressers (defer to 2A)"
        )
    return report


def _print_summary(results: dict) -> None:
    print(f"\n=== Mentions backtest: {results['n_samples']} samples ===")
    print(f"skipped: {results['skipped']}")
    print(f"base yes-rate: {results['base_yes_rate']}")
    over = results["overall"]
    print(f"overall   Brier={over['brier']} (naive={over['naive_brier']}) hit-rate={over['hit_rate']}")
    print(f"gdelt-only Brier={results['gdelt_only']['brier']} hit-rate={results['gdelt_only']['hit_rate']} (n={results['gdelt_only']['n']})")
    print(f"corpus     Brier={results['corpus_backed']['brier']} hit-rate={results['corpus_backed']['hit_rate']} (n={results['corpus_backed']['n']})")
    print("\nCalibration (GDELT-only) — predicted band vs actual yes-rate:")
    print(f"  {'bin':<14} {'count':>6} {'mean_pred':>10} {'actual':>8}")
    for row in results["gdelt_only"]["calibration"]:
        print(f"  {row['bin']:<14} {row['count']:>6} {str(row['mean_predicted']):>10} {str(row['actual_yes_rate']):>8}")

    fed_report = results.get("fed_corpus_report")
    if fed_report:
        print("\n=== 2B Fed-corpus tie-in (KXFEDMENTION only) ===")
        if fed_report.get("n_fed", 0) == 0:
            print(f"  {fed_report.get('note')}")
            return
        print(f"  n_fed={fed_report['n_fed']}  corpus_backed={fed_report['n_corpus_backed']}  "
              f"gdelt_only={fed_report['n_gdelt_only']}  fed_yes_rate={fed_report['fed_yes_rate']}")
        print(f"  Brier  naive={fed_report['naive_brier']}  "
              f"gdelt_only={fed_report['gdelt_only_brier']}  "
              f"corpus_backed={fed_report['corpus_backed_brier']}")
        interval = fed_report.get("corpus_backed_brier_credible_interval")
        if interval:
            print(f"  corpus-backed Brier 95% CI: "
                  f"[{interval['percentile_2_5']}, {interval['percentile_97_5']}]")
        if "caveat" in fed_report:
            print(f"  NOTE: {fed_report['caveat']}")


def _main() -> None:
    parser = argparse.ArgumentParser(description="Backtest mentions signal calibration (read-only)")
    parser.add_argument("--series", nargs="*", default=DEFAULT_SERIES,
                        help="Mentions series tickers to backtest")
    parser.add_argument("--max-per-series", type=int, default=300,
                        help="Cap settled markets fetched per series")
    parser.add_argument("--out", default="/tmp/mentions_backtest.json")
    parser.add_argument("--fed-corpus-asof", action="store_true",
                        help="2B: count Powell's pressers as-of close for KXFEDMENTION markets")
    parser.add_argument("--db", default=None,
                        help="Dedicated archive db for --fed-corpus-asof (NOT the production archive)")
    args = parser.parse_args()
    asyncio.run(run(
        args.series, args.max_per_series, args.out,
        fed_corpus_asof=args.fed_corpus_asof, db_path=args.db,
    ))


if __name__ == "__main__":
    _main()
