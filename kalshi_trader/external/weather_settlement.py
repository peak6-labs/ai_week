"""Resolve a Kalshi weather series to the station its contract settles on.

The live-observation override (see
``thoughts/shared/plans/2026-06-03-live-observation-override-weather.md``) must
read the realized extreme off the contract's *actual* settlement station — not a
hardcoded airport. Settlement is not uniform: ``KXHIGHLAX`` settles on the NWS
Climatological Report issued by LAX, while ``KXTEMPNYCH`` settles on AccuWeather
(no queryable station). This module inspects a series' ``settlement_sources``
(fetched by ``scripts/market_rules.py`` and cached in
``series_contract_terms.json``) and returns the station id when one is
structurally derivable, or flags a non-station source so the caller disables the
override rather than guessing.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

# Source types we can recognize. Only ``nws_station`` carries a queryable station.
SOURCE_TYPE_NWS_STATION = "nws_station"
SOURCE_TYPE_ACCUWEATHER = "accuweather"
SOURCE_TYPE_UNKNOWN = "unknown"


def _station_from_source(settlement_source: dict) -> dict | None:
    """Classify a single ``{name, url}`` settlement source.

    Returns a dict with ``source_type`` and (for NWS stations) ``station_id``, or
    ``None`` when the URL is missing/unparseable.
    """
    url = (settlement_source or {}).get("url") or ""
    if not url:
        return None

    parsed_url = urlparse(url)
    host = (parsed_url.netloc or "").lower()
    source_name = (settlement_source or {}).get("name")

    # NWS Climatological Report product, e.g.
    # forecast.weather.gov/product.php?site=LOX&product=CLI&issuedby=LAX
    # The settlement station is the ``issuedby`` office/station code.
    if host.endswith("weather.gov"):
        query_parameters = parse_qs(parsed_url.query)
        issued_by_values = query_parameters.get("issuedby") or []
        if issued_by_values and issued_by_values[0].strip():
            return {
                "source_type": SOURCE_TYPE_NWS_STATION,
                "station_id": issued_by_values[0].strip().upper(),
                "source_name": source_name,
            }
        # api.weather.gov/stations/<ID>/observations — station id is in the path.
        path_parts = [part for part in parsed_url.path.split("/") if part]
        if "stations" in path_parts:
            station_index = path_parts.index("stations") + 1
            if station_index < len(path_parts):
                return {
                    "source_type": SOURCE_TYPE_NWS_STATION,
                    "station_id": path_parts[station_index].strip().upper(),
                    "source_name": source_name,
                }
        return {"source_type": SOURCE_TYPE_UNKNOWN, "station_id": None, "source_name": source_name}

    if "accuweather" in host:
        return {
            "source_type": SOURCE_TYPE_ACCUWEATHER,
            "station_id": None,
            "source_name": source_name,
        }

    return {"source_type": SOURCE_TYPE_UNKNOWN, "station_id": None, "source_name": source_name}


def resolve_settlement_station(
    series_ticker: str, settlement_sources: list[dict] | None
) -> dict | None:
    """Resolve a series' settlement station from its settlement sources.

    Args:
        series_ticker: The series prefix (e.g. ``KXHIGHLAX``). Carried through for
            logging/metadata only — the decision is driven by the sources.
        settlement_sources: List of ``{name, url}`` dicts from the series'
            contract terms (``series_contract_terms.json``).

    Returns:
        - ``{"series_ticker", "source_type": "nws_station", "station_id", "source_name"}``
          when a station is structurally derivable (the override may run).
        - ``{"series_ticker", "source_type": <non-station>, "station_id": None, ...}``
          for a recognized non-station source (e.g. AccuWeather) — the caller
          disables the override.
        - ``None`` when there are no sources or none are recognizable.

    A source that yields a station wins over a recognized non-station source,
    which in turn wins over an unknown one.
    """
    if not settlement_sources:
        return None

    best_non_station: dict | None = None
    for settlement_source in settlement_sources:
        classified = _station_from_source(settlement_source)
        if classified is None:
            continue
        if classified.get("station_id"):
            return {"series_ticker": series_ticker.upper(), **classified}
        # Remember the first recognized (non-unknown) non-station source as a
        # fallback answer if no station turns up.
        if best_non_station is None and classified["source_type"] != SOURCE_TYPE_UNKNOWN:
            best_non_station = classified

    if best_non_station is not None:
        return {"series_ticker": series_ticker.upper(), **best_non_station}
    return None
