"""On-disk cache of per-series settlement-station coordinates.

A Kalshi temperature contract settles on a specific NWS station's reading, but
the GEFS ensemble was being forecast at the city *centroid* (downtown) rather
than at the settlement station — downtown-LA vs LAX manufactured spurious edges.
This module resolves a series to the ``(lat, lon)`` of the station it settles on
and caches the answer by series ticker, mirroring the ``series_slugs.json``
(:mod:`kalshi_trader.web_links`) and ``series_contract_terms.json``
(:mod:`kalshi_trader.contract_terms`) caches.

Resolution is cache-first: a hit makes zero API calls. On a miss it reads the
series' cached settlement terms, resolves the station via
:func:`kalshi_trader.external.weather_settlement.resolve_settlement_station`,
fetches the station geometry via ``NOAAClient.get_station_coordinates``, and
persists the result. Series that settle on a non-NWS source (AccuWeather) or
resolve to no station cache a *negative* entry (``station_id: null``) and return
``None`` so the caller falls back to the centroid without re-resolving. A series
with no cached terms at all returns ``None`` *without* caching, so it can resolve
once the terms are fetched.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from kalshi_trader.contract_terms import load_contract_terms
from kalshi_trader.external.weather_settlement import resolve_settlement_station

SETTLEMENT_STATIONS_PATH = Path(__file__).with_name("series_settlement_stations.json")


def load_station_coordinates(path: Path | str = SETTLEMENT_STATIONS_PATH) -> dict[str, dict]:
    """Load cached station coordinates keyed by uppercase series ticker."""
    coordinates_path = Path(path)
    if not coordinates_path.exists():
        return {}
    raw = json.loads(coordinates_path.read_text())
    if not isinstance(raw, dict):
        return {}
    return {str(series_ticker).upper(): entry for series_ticker, entry in raw.items()}


def save_station_coordinates(
    coordinates: dict[str, dict], path: Path | str = SETTLEMENT_STATIONS_PATH
) -> None:
    """Persist station coordinates in stable sorted order (one entry per series)."""
    clean = {str(series_ticker).upper(): entry for series_ticker, entry in coordinates.items()}
    Path(path).write_text(json.dumps(dict(sorted(clean.items())), indent=2) + "\n")


def station_label_for_series(
    series_ticker: str, path: Path | str = SETTLEMENT_STATIONS_PATH
) -> str:
    """``station:<id>`` when a settlement station is cached for the series, else
    ``centroid`` — the ``forecast_point`` label stamped on weather signals."""
    series_prefix = series_ticker.split("-", 1)[0].upper()
    entry = load_station_coordinates(path).get(series_prefix)
    if entry and entry.get("station_id"):
        return f"station:{entry['station_id']}"
    return "centroid"


async def resolve_station_coordinates(
    series_ticker: str,
    client,
    *,
    path: Path | str = SETTLEMENT_STATIONS_PATH,
) -> tuple[float, float] | None:
    """Return the ``(lat, lon)`` of a series' settlement station, or ``None``.

    Cache-first; on a miss resolves the station from the series' cached contract
    terms and fetches its geometry via ``client.get_station_coordinates``. Returns
    ``None`` (and falls back to the city centroid) for AccuWeather / no-station
    series — those are cached as negatives so they are not re-resolved — and for
    series whose terms are not yet cached (those are *not* cached, so they resolve
    once the terms arrive).
    """
    series_prefix = series_ticker.split("-", 1)[0].upper()
    cache = load_station_coordinates(path)
    if series_prefix in cache:
        entry = cache[series_prefix]
        latitude, longitude = entry.get("lat"), entry.get("lon")
        if latitude is not None and longitude is not None:
            return float(latitude), float(longitude)
        return None

    terms_entry = load_contract_terms().get(series_prefix)
    resolved = resolve_settlement_station(series_prefix, (terms_entry or {}).get("settlement_sources"))
    if resolved is None:
        # No terms cached yet / unrecognized source — don't cache (the terms may
        # arrive on a later cycle), just fall back to the centroid for now.
        return None

    resolved_at = datetime.now(tz=timezone.utc).isoformat()
    station_id = resolved.get("station_id")
    if not station_id:
        # Recognized non-station source (e.g. AccuWeather): cache the negative so
        # we never re-resolve it, and fall back to the centroid.
        cache[series_prefix] = {
            "station_id": None, "lat": None, "lon": None,
            "source_type": resolved.get("source_type"), "resolved_at": resolved_at,
        }
        save_station_coordinates(cache, path)
        return None

    coordinates = await client.get_station_coordinates(station_id)
    if coordinates is None:
        # Station id resolved but geometry fetch failed — don't cache so a later
        # cycle can retry; fall back to the centroid this time.
        return None

    latitude, longitude = coordinates
    cache[series_prefix] = {
        "station_id": station_id, "lat": latitude, "lon": longitude,
        "source_type": resolved.get("source_type"), "resolved_at": resolved_at,
    }
    save_station_coordinates(cache, path)
    return latitude, longitude
