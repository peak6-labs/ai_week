#!/usr/bin/env python
"""Standard weather deliverable: a ranked edge table over live Kalshi weather
markets (READ-ONLY — never places an order).

This is the canonical output a weather run should produce. It enumerates the
weather series, pulls their live open markets (with normalized yes_bid/yes_ask),
scores each one deterministically against the GEFS ensemble taken at the
contract's **settlement station** (not the city centroid), picks the better side,
ranks by edge, and writes ``reports/weather-scan-<TS>.md`` (top 25 by edge, with
live bid/ask) plus the full scored JSON alongside.

It reuses the audited fixes: round-to-settlement bucketing (signals/weather.py),
station-coordinate resolution (station_coords.py), and the bounded+retried
fetch pattern (contract_terms.py / _retry.py). Forecasts come from NOAA /
Open-Meteo; quotes from Kalshi. It needs ``KALSHI_ENV=prod`` for real liquidity
but issues **no** writes to the exchange.

Usage:
    KALSHI_ENV=prod python scripts/weather_scan.py [--top-n 25] [--limit-series N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date as date_type, datetime, timezone
from pathlib import Path

import kalshi_trader.config  # noqa: F401 — loads .env so API keys / KALSHI_ENV are set
from kalshi_trader._retry import with_retry
from kalshi_trader.client import KalshiClient
from kalshi_trader.contract_terms import load_contract_terms
from kalshi_trader.external.noaa import NOAAClient
from kalshi_trader.external.open_meteo import OpenMeteoClient
from kalshi_trader.external.weather_parser import (
    _city_from_ticker,
    _metric_from_ticker,
    parse_title,
)
from kalshi_trader.external.weather_settlement import resolve_settlement_station
from kalshi_trader.signals.weather import build_ensemble_signal, observation_lock_fraction
from kalshi_trader.station_coords import resolve_station_coordinates, station_label_for_series
from kalshi_trader.weather_report import (
    ScoredWeatherMarket,
    is_suspect_edge,
    rank_and_render,
    score_sides,
)

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
# Only metrics the ensemble signal scores (wind is parsed but not modelled here).
_SCORED_METRICS = {"temp_high", "temp_low", "precipitation"}
_SERIES_CONCURRENCY = 6
_EVENT_CONCURRENCY = 6


def _series_prefix(ticker: str) -> str:
    return ticker.split("-", 1)[0].upper()


def _is_weather_series(series_ticker: str) -> bool:
    """A series is in scope when its ticker encodes a scored weather metric and a
    known city (e.g. ``KXHIGHLAX``, ``KXLOWTATL``, ``KXRAINCHI``)."""
    return (
        _metric_from_ticker(series_ticker) in _SCORED_METRICS
        and _city_from_ticker(series_ticker) is not None
    )


async def _weather_series_tickers(client: KalshiClient, limit: int | None) -> list[str]:
    """List the open weather series tickers, classified from the series catalog."""
    series_objects = await with_retry(client.get_series)
    weather = sorted(
        {
            str(series.get("ticker"))
            for series in series_objects
            if series.get("ticker") and _is_weather_series(str(series.get("ticker")))
        }
    )
    return weather[:limit] if limit else weather


async def _open_markets_for_series(client: KalshiClient, series_ticker: str) -> list[dict]:
    """Every open market (strike) for one series, normalized (yes_bid/yes_ask)."""
    markets: list[dict] = []
    cursor = ""
    while True:
        response = await with_retry(
            client.get_markets, status="open", series_ticker=series_ticker,
            cursor=cursor, limit=1000,
        )
        markets.extend(response.get("markets") or [])
        cursor = response.get("cursor") or ""
        if not cursor:
            break
    return markets


def _station_id_for_series(series_prefix: str) -> str | None:
    """Resolve the series' NWS settlement station id from cached terms, or None."""
    terms_entry = load_contract_terms().get(series_prefix)
    resolved = resolve_settlement_station(series_prefix, (terms_entry or {}).get("settlement_sources"))
    return resolved.get("station_id") if resolved else None


async def _score_event(
    *,
    noaa: NOAAClient,
    open_meteo: OpenMeteoClient,
    series_prefix: str,
    target_date: date_type,
    metric: str,
    centroid: tuple[float, float],
    markets: list[dict],
    today: date_type,
) -> list[ScoredWeatherMarket]:
    """Score every strike in one (series, date, metric) event off a single forecast.

    Fetches the ensemble once at the settlement station (centroid fallback) and,
    for a same-day event, the realized extreme once; then builds the ensemble
    signal per strike and scores the better side against its live quote.
    """
    try:
        station_coords = await resolve_station_coordinates(series_prefix, noaa)
    except Exception:
        station_coords = None
    forecast_lat, forecast_lon = station_coords or centroid
    forecast_point = station_label_for_series(series_prefix) if station_coords else "centroid"

    ensemble = await open_meteo.get_ensemble_members(forecast_lat, forecast_lon, target_date, metric)

    observation: dict | None = None
    lock_fraction = 0.0
    if target_date == today:
        station_id = _station_id_for_series(series_prefix)
        if station_id:
            try:
                observation = await noaa.get_observed_extreme(station_id, target_date, metric)
                lock_fraction = observation_lock_fraction(metric, observation)
            except Exception:
                observation = None

    scored: list[ScoredWeatherMarket] = []
    for market in markets:
        ticker = market.get("ticker")
        parsed = parse_title(ticker, market.get("title") or "")
        if not parsed or parsed.get("metric") != metric:
            continue
        estimate = build_ensemble_signal(
            ticker, metric, parsed["threshold"], parsed["operator"], ensemble,
            parsed.get("threshold_high"), observation, lock_fraction,
        )
        if estimate.metadata.get("data_quality") == "empty":
            continue  # no usable ensemble — skip rather than emit a 0.5 placeholder
        yes_bid = market.get("yes_bid")
        yes_ask = market.get("yes_ask")
        side, edge_cents, fair_cents = score_sides(estimate.probability, yes_bid, yes_ask)
        volume_24h = float(market.get("volume_24h") or 0)
        scored.append(
            ScoredWeatherMarket(
                ticker=ticker,
                forecast_point=forecast_point,
                side=side,
                model_probability=estimate.probability,
                fair_cents=fair_cents,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                edge_cents=edge_cents,
                volume_24h=volume_24h,
                city=parsed.get("city") or "",
                metric=metric,
                suspect=is_suspect_edge(edge_cents, volume_24h),
                metadata={
                    "members_satisfying": estimate.metadata.get("members_satisfying"),
                    "member_count": estimate.metadata.get("member_count"),
                    "members_clamped": estimate.metadata.get("members_clamped"),
                    "realized_extreme": estimate.metadata.get("realized_extreme"),
                    "target_date": target_date.isoformat(),
                },
            )
        )
    return scored


async def _run(top_n: int, limit_series: int | None) -> None:
    client = KalshiClient()
    noaa = NOAAClient()
    open_meteo = OpenMeteoClient()
    today = datetime.now(tz=timezone.utc).date()

    try:
        series_tickers = await _weather_series_tickers(client, limit_series)
        print(f"Scanning {len(series_tickers)} weather series (READ-ONLY, no orders)...")

        # Fetch each series' open markets, bounded + retried.
        series_semaphore = asyncio.Semaphore(_SERIES_CONCURRENCY)

        async def _markets(series_ticker: str) -> tuple[str, list[dict]]:
            async with series_semaphore:
                try:
                    return series_ticker, await _open_markets_for_series(client, series_ticker)
                except Exception:
                    return series_ticker, []

        markets_by_series = dict(await asyncio.gather(*[_markets(s) for s in series_tickers]))

        # Group every open strike into (series, date, metric) events, carrying the
        # city centroid as the forecast fallback.
        events: dict[tuple[str, str, str], dict] = {}
        for series_ticker, markets in markets_by_series.items():
            for market in markets:
                ticker = market.get("ticker")
                parsed = parse_title(ticker, market.get("title") or "")
                if not parsed or parsed.get("metric") not in _SCORED_METRICS:
                    continue
                key = (_series_prefix(ticker), parsed["target_date"], parsed["metric"])
                event = events.setdefault(
                    key, {"centroid": (parsed["lat"], parsed["lon"]), "markets": []}
                )
                event["markets"].append(market)

        print(f"Found {len(events)} city/metric/date events across "
              f"{sum(len(event['markets']) for event in events.values())} open strikes.")

        # Score each event off a single forecast, bounded + retried.
        event_semaphore = asyncio.Semaphore(_EVENT_CONCURRENCY)

        async def _scored(key, event) -> list[ScoredWeatherMarket]:
            series_prefix, target_iso, metric = key
            async with event_semaphore:
                try:
                    return await _score_event(
                        noaa=noaa, open_meteo=open_meteo, series_prefix=series_prefix,
                        target_date=date_type.fromisoformat(target_iso), metric=metric,
                        centroid=event["centroid"], markets=event["markets"], today=today,
                    )
                except Exception as caught_exception:
                    print(f"  ! {series_prefix} {target_iso} {metric}: {caught_exception}")
                    return []

        scored_groups = await asyncio.gather(*[_scored(key, event) for key, event in events.items()])
        scored_rows = [row for group in scored_groups for row in group]
    finally:
        await client.aclose()
        await noaa.close()
        await open_meteo.close()

    generated_at = datetime.now(tz=timezone.utc)
    report = rank_and_render(scored_rows, top_n, generated_at=generated_at)
    print("\n" + report)

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    report_path = _REPORTS_DIR / f"weather-scan-{stamp}.md"
    json_path = _REPORTS_DIR / f"weather-scan-{stamp}.json"
    report_path.write_text(report)
    json_path.write_text(
        json.dumps(
            [asdict(row) for row in sorted(scored_rows, key=lambda r: r.edge_cents, reverse=True)],
            indent=2, default=str,
        ) + "\n"
    )
    print(f"Wrote {report_path}")
    print(f"Wrote {json_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ranked weather edge scan")
    parser.add_argument("--top-n", type=int, default=25, help="Rows in the ranked table (default 25)")
    parser.add_argument("--limit-series", type=int, default=None,
                        help="Cap the number of weather series scanned (for a quick run)")
    args = parser.parse_args()
    asyncio.run(_run(args.top_n, args.limit_series))


if __name__ == "__main__":
    main()
