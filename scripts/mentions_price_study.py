#!/usr/bin/env python3
"""Q6 mispricing study: is the edge in the *price* rather than in any model?

Read-only. Reads settled-market price history from Kalshi (no orders) and joins it
onto the existing GDELT backtest sample at ``/tmp/mentions_backtest.json`` (which
already carries ``ticker, series, realized, p_gdelt, close_time``). For each settled
mentions market it picks a representative market price as-of shortly before close and
asks:

  (a) how good is the **market price** itself as a predictor (Brier) — the bar any
      future signal must beat — vs the naive base rate and vs the discredited GDELT
      ``p_gdelt`` model, all on the same price-available subset;
  (b) reliability diagram of price vs realized, overall and per subtype;
  (c) favorite-longshot bias (do longshots realize richer, favorites poorer?);
  (d) does ``p_gdelt`` (the "ubiquity" heuristic) add anything over the price? — a
      logistic regression ``realized ~ price`` vs ``realized ~ price + p_gdelt`` with
      a likelihood-ratio test, plus the raw price/ubiquity correlation.

Reusing the existing sample (rather than re-fetching settled markets) keeps the
``p_gdelt`` join stable as markets age out of Kalshi's settled window.

Usage:
  KALSHI_ENV=prod PYTHONPATH=. .venv/bin/python scripts/mentions_price_study.py \
    --samples /tmp/mentions_backtest.json --out /tmp/mentions_price_study.json \
    --asof-minutes 60
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

import kalshi_trader.config  # noqa: F401 — loads .env / KALSHI_ENV
from kalshi_trader.client import KalshiClient
from kalshi_trader.mentions.price_study import (
    bootstrap_coefficient_intervals,
    brier_score,
    favorite_longshot_table,
    fit_logistic_regression,
    likelihood_ratio_test,
    logistic_log_likelihood,
    logistic_predicted_probabilities,
    pearson_correlation,
    reliability_table,
    select_asof_price,
    standardize_features,
)

import numpy

# Sports/entertainment broadcast venues (national-news GDELT proxy); everything else
# (chiefly KXFEDMENTION) is the politics subtype. Mirrors mentions_backtest.py:54.
SPORTS_SERIES_PREFIXES = ("KXNBA", "KXNHL", "KXMLB", "KXNFL", "KXUFC", "KXLOVEISL")
MINIMUM_ROWS_FOR_REGRESSION = 20


def parse_iso_to_epoch_seconds(value: str) -> int | None:
    """Parse a Kalshi ISO timestamp (``...Z``) to a Unix epoch second, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _subtype_for_series(series_ticker: str) -> str:
    return "sports" if str(series_ticker or "").startswith(SPORTS_SERIES_PREFIXES) else "politics"


def _extract_candle_list(api_response: dict, ticker: str) -> list[dict]:
    """Pull the candle list out of the candlesticks response.

    The live batch shape is ``{"markets": [{"market_ticker", "candlesticks": [...]}]}``;
    a couple of legacy/flat shapes are tolerated defensively.
    """
    if not isinstance(api_response, dict):
        return []
    markets = api_response.get("markets")
    if isinstance(markets, list):
        for entry in markets:
            if isinstance(entry, dict) and (entry.get("market_ticker") == ticker or len(markets) == 1):
                return entry.get("candlesticks") or []
    for key in ("candlesticks", "market_candlesticks", "candles"):
        value = api_response.get(key)
        if isinstance(value, list):
            return value
    return []


def _dollar_string_to_cents(dollar_value) -> float | None:
    """Kalshi candle prices are dollar strings (``"0.5300"`` = 53¢). Return cents."""
    if dollar_value is None or dollar_value == "":
        return None
    try:
        return float(dollar_value) * 100.0
    except (TypeError, ValueError):
        return None


def _normalize_candle(raw_candle: dict) -> dict:
    """Map a raw Kalshi candle to ``{end_period_ts, price_close, price_mean, volume}``.

    Prices come as dollar strings under ``price.{close_dollars, mean_dollars}``; the
    no-trade fallback ``price_mean`` is the live yes-book midpoint
    (``yes_bid.close_dollars`` / ``yes_ask.close_dollars``), which is a meaningful
    price even in a period with no trades, rather than a stale carried mean. All
    prices are returned in cents (0–100); select_asof_price converts to probability.
    """
    price_object = raw_candle.get("price") if isinstance(raw_candle.get("price"), dict) else {}
    price_close = _dollar_string_to_cents(price_object.get("close_dollars"))

    yes_bid = raw_candle.get("yes_bid") if isinstance(raw_candle.get("yes_bid"), dict) else {}
    yes_ask = raw_candle.get("yes_ask") if isinstance(raw_candle.get("yes_ask"), dict) else {}
    yes_bid_cents = _dollar_string_to_cents(yes_bid.get("close_dollars"))
    yes_ask_cents = _dollar_string_to_cents(yes_ask.get("close_dollars"))
    if yes_bid_cents is not None and yes_ask_cents is not None:
        price_mean = (yes_bid_cents + yes_ask_cents) / 2.0
    else:
        price_mean = _dollar_string_to_cents(price_object.get("mean_dollars"))

    volume_value = raw_candle.get("volume_fp", raw_candle.get("volume"))
    try:
        volume = float(volume_value) if volume_value is not None else 0.0
    except (TypeError, ValueError):
        volume = 0.0

    return {
        "end_period_ts": raw_candle.get("end_period_ts"),
        "price_close": price_close,
        "price_mean": price_mean,
        "volume": volume,
    }


async def _fetch_candles(
    kalshi_client: KalshiClient, ticker: str, start_epoch_seconds: int, end_epoch_seconds: int,
    *, log_raw_shape: bool,
) -> list[dict]:
    api_response = await kalshi_client.get_market_candlesticks_batch(
        tickers=[ticker], start_ts=start_epoch_seconds, end_ts=end_epoch_seconds, period_interval=60,
    )
    if log_raw_shape:
        top_keys = list(api_response.keys()) if isinstance(api_response, dict) else type(api_response)
        print(f"  [shape probe] {ticker}: top-level keys = {top_keys}", file=sys.stderr)
    raw_candles = _extract_candle_list(api_response, ticker)
    if log_raw_shape and raw_candles:
        print(f"  [shape probe] first candle = {raw_candles[0]}", file=sys.stderr)
    return [_normalize_candle(candle) for candle in raw_candles if isinstance(candle, dict)]


async def run(
    samples_path: str, out_path: str, *, asof_minutes: int, lookback_hours: int,
) -> dict:
    with open(samples_path) as handle:
        sample_file = json.load(handle)
    samples = sample_file.get("samples", []) or []
    if not samples:
        raise SystemExit(f"{samples_path} has no samples — run scripts/mentions_backtest.py first")

    rows: list[dict] = []
    skipped = {"no_close_time": 0, "candle_error": 0, "price_missing": 0}
    kalshi_client = KalshiClient()
    async with kalshi_client:
        for sample_index, sample in enumerate(samples):
            close_epoch_seconds = parse_iso_to_epoch_seconds(sample.get("close_time", ""))
            if close_epoch_seconds is None:
                skipped["no_close_time"] += 1
                continue
            start_epoch_seconds = close_epoch_seconds - lookback_hours * 3600
            try:
                candles = await _fetch_candles(
                    kalshi_client, sample.get("ticker", ""), start_epoch_seconds, close_epoch_seconds,
                    log_raw_shape=(sample_index == 0),
                )
                await asyncio.sleep(0.2)  # polite spacing
            except Exception as caught_exception:
                skipped["candle_error"] += 1
                print(f"    candle error {sample.get('ticker')}: {caught_exception}", file=sys.stderr)
                continue

            asof_price = select_asof_price(candles, close_epoch_seconds, asof_minutes)
            if asof_price is None:
                skipped["price_missing"] += 1
                continue
            rows.append({
                "ticker": sample.get("ticker"),
                "series": sample.get("series"),
                "subtype": _subtype_for_series(sample.get("series", "")),
                "realized_outcome": int(sample.get("realized", 0)),
                "market_price": asof_price["market_price"],
                "price_source": asof_price["price_source"],
                "p_gdelt": float(sample.get("p_gdelt", 0.0) or 0.0),
                "volume": sample.get("volume", 0),
            })

    results = _compute_metrics(rows, skipped, samples_path, sample_file.get("generated_at"),
                               asof_minutes, lookback_hours)
    with open(out_path, "w") as handle:
        json.dump(results, handle, indent=2, default=str)
    _print_summary(results)
    return results


def _subset_metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    base_yes_rate = float(numpy.mean([row["realized_outcome"] for row in rows]))
    naive_rows = [{"realized_outcome": row["realized_outcome"], "prediction": base_yes_rate} for row in rows]
    return {
        "n": len(rows),
        "base_yes_rate": round(base_yes_rate, 4),
        "market_brier": round(brier_score(rows, "market_price"), 4),
        "p_gdelt_brier": round(brier_score(rows, "p_gdelt"), 4),
        "naive_brier": round(brier_score(naive_rows, "prediction"), 4),
        "reliability": reliability_table(rows),
        "favorite_longshot": favorite_longshot_table(rows),
    }


def _regression_block(rows: list[dict]) -> dict:
    """Logistic ``realized ~ price`` vs ``realized ~ price + p_gdelt`` + LR test."""
    if len(rows) < MINIMUM_ROWS_FOR_REGRESSION:
        return {"skipped": f"only {len(rows)} rows (< {MINIMUM_ROWS_FOR_REGRESSION})"}
    outcomes = numpy.array([row["realized_outcome"] for row in rows], dtype=float)
    market_price = numpy.array([row["market_price"] for row in rows], dtype=float)
    p_gdelt = numpy.array([row["p_gdelt"] for row in rows], dtype=float)

    price_only, _, _ = standardize_features(market_price.reshape(-1, 1))
    price_and_gdelt, _, _ = standardize_features(numpy.column_stack([market_price, p_gdelt]))

    coefficients_reduced = fit_logistic_regression(price_only, outcomes)
    coefficients_full = fit_logistic_regression(price_and_gdelt, outcomes)
    log_likelihood_reduced = logistic_log_likelihood(price_only, outcomes, coefficients_reduced)
    log_likelihood_full = logistic_log_likelihood(price_and_gdelt, outcomes, coefficients_full)

    intervals_full = bootstrap_coefficient_intervals(price_and_gdelt, outcomes)
    p_gdelt_interval = intervals_full[2] if len(intervals_full) >= 3 else None
    return {
        "model_price": {
            "standardized_coefficients": [round(value, 4) for value in coefficients_reduced.tolist()],
            "in_sample_brier": round(brier_score(
                [{"realized_outcome": o, "prediction": p} for o, p in zip(
                    outcomes, logistic_predicted_probabilities(price_only, coefficients_reduced))],
                "prediction"), 4),
        },
        "model_price_plus_gdelt": {
            "standardized_coefficients": [round(value, 4) for value in coefficients_full.tolist()],
            "p_gdelt_coefficient_interval": p_gdelt_interval,
            "in_sample_brier": round(brier_score(
                [{"realized_outcome": o, "prediction": p} for o, p in zip(
                    outcomes, logistic_predicted_probabilities(price_and_gdelt, coefficients_full))],
                "prediction"), 4),
        },
        "likelihood_ratio_p_value": round(
            likelihood_ratio_test(log_likelihood_full, log_likelihood_reduced, degrees_of_freedom=1), 4),
        "price_vs_gdelt_correlation": round(pearson_correlation(
            market_price.tolist(), p_gdelt.tolist()), 4),
    }


def _compute_metrics(rows, skipped, samples_path, samples_generated_at, asof_minutes, lookback_hours) -> dict:
    sports_rows = [row for row in rows if row["subtype"] == "sports"]
    politics_rows = [row for row in rows if row["subtype"] == "politics"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "samples_source": samples_path,
        "samples_generated_at": samples_generated_at,
        "asof_minutes": asof_minutes,
        "lookback_hours": lookback_hours,
        "price_available_n": len(rows),
        "skipped": skipped,
        "overall": _subset_metrics(rows),
        "sports": _subset_metrics(sports_rows),
        "politics": _subset_metrics(politics_rows),
        "ubiquity_test": _regression_block(rows),
        "rows": rows,
    }


def _print_calibration(label: str, metrics: dict) -> None:
    if not metrics.get("n"):
        print(f"\n{label}: no price-available rows")
        return
    print(f"\n{label}: n={metrics['n']}  base_yes_rate={metrics['base_yes_rate']}")
    print(f"  Brier  market={metrics['market_brier']}  naive={metrics['naive_brier']}  "
          f"p_gdelt={metrics['p_gdelt_brier']}")
    print(f"  {'bin':<14} {'count':>6} {'mean_price':>11} {'realized':>9} {'bias':>8}")
    for row in metrics["favorite_longshot"]:
        print(f"  {row['bin']:<14} {row['count']:>6} {str(row['mean_price']):>11} "
              f"{str(row['realized_yes_rate']):>9} {str(row['signed_bias']):>8}")


def _print_summary(results: dict) -> None:
    print(f"\n=== Q6 mispricing study (as-of {results['asof_minutes']} min before close) ===")
    print(f"price-available: {results['price_available_n']} / skipped {results['skipped']}")
    _print_calibration("OVERALL", results["overall"])
    _print_calibration("SPORTS", results["sports"])
    _print_calibration("POLITICS", results["politics"])
    test = results["ubiquity_test"]
    if "skipped" in test:
        print(f"\nUbiquity test: {test['skipped']}")
    else:
        print(f"\nUbiquity test (does p_gdelt add over price?):")
        print(f"  price-only Brier={test['model_price']['in_sample_brier']}  "
              f"price+gdelt Brier={test['model_price_plus_gdelt']['in_sample_brier']}")
        print(f"  LR test p-value={test['likelihood_ratio_p_value']}  "
              f"corr(price, p_gdelt)={test['price_vs_gdelt_correlation']}")
        interval = test['model_price_plus_gdelt']['p_gdelt_coefficient_interval']
        if interval:
            print(f"  p_gdelt standardized coef 95% CI: "
                  f"[{interval['percentile_2_5']:.4f}, {interval['percentile_97_5']:.4f}]")
    print("\nRead: if market_brier already beats naive_brier AND p_gdelt_brier, the edge "
          "is in the price — a signal must beat the market, not the naive line.\n")


def _main() -> None:
    parser = argparse.ArgumentParser(description="Q6 mentions mispricing study (read-only)")
    parser.add_argument("--samples", default="/tmp/mentions_backtest.json")
    parser.add_argument("--out", default="/tmp/mentions_price_study.json")
    parser.add_argument("--asof-minutes", type=int, default=60,
                        help="Use the latest price at/before this many minutes before close")
    parser.add_argument("--lookback-hours", type=int, default=48,
                        help="How far back to request candles (illiquid markets trade rarely)")
    args = parser.parse_args()
    asyncio.run(run(args.samples, args.out, asof_minutes=args.asof_minutes,
                    lookback_hours=args.lookback_hours))


if __name__ == "__main__":
    _main()
