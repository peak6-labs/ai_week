"""Tests for kalshi_trader/external/weather_settlement.py and the
kalshi_trader/station_coords.py settlement-station coordinate cache."""
from __future__ import annotations

import asyncio

from kalshi_trader import station_coords
from kalshi_trader.external.weather_settlement import (
    SOURCE_TYPE_ACCUWEATHER,
    SOURCE_TYPE_NWS_STATION,
    resolve_settlement_station,
)


def test_resolve_nws_climatological_report_returns_issuedby_station():
    # KXHIGHLAX-style settlement source: the station is the ``issuedby`` code.
    sources = [
        {
            "name": "NWS Climatological Report",
            "url": "https://forecast.weather.gov/product.php?site=LOX&product=CLI&issuedby=LAX",
        }
    ]
    resolved = resolve_settlement_station("KXHIGHLAX", sources)
    assert resolved is not None
    assert resolved["source_type"] == SOURCE_TYPE_NWS_STATION
    assert resolved["station_id"] == "LAX"
    assert resolved["series_ticker"] == "KXHIGHLAX"


def test_resolve_api_weather_gov_stations_path():
    sources = [
        {"name": "NWS Observations", "url": "https://api.weather.gov/stations/KATL/observations"}
    ]
    resolved = resolve_settlement_station("KXLOWTATL", sources)
    assert resolved is not None
    assert resolved["source_type"] == SOURCE_TYPE_NWS_STATION
    assert resolved["station_id"] == "KATL"


def test_resolve_accuweather_disables_override():
    # KXTEMPNYCH settles on AccuWeather — no queryable station; override disabled.
    sources = [{"name": "AccuWeather", "url": "https://www.accuweather.com"}]
    resolved = resolve_settlement_station("KXTEMPNYCH", sources)
    assert resolved is not None
    assert resolved["source_type"] == SOURCE_TYPE_ACCUWEATHER
    assert resolved["station_id"] is None


def test_resolve_prefers_station_over_non_station_source():
    sources = [
        {"name": "AccuWeather", "url": "https://www.accuweather.com"},
        {
            "name": "NWS Climatological Report",
            "url": "https://forecast.weather.gov/product.php?product=CLI&issuedby=ORD",
        },
    ]
    resolved = resolve_settlement_station("KXHIGHCHI", sources)
    assert resolved is not None
    assert resolved["station_id"] == "ORD"


def test_resolve_empty_sources_returns_none():
    assert resolve_settlement_station("KXFOO", []) is None
    assert resolve_settlement_station("KXFOO", None) is None


def test_resolve_unknown_source_only_returns_none():
    sources = [{"name": "Mystery", "url": "https://example.com/whatever"}]
    assert resolve_settlement_station("KXFOO", sources) is None


def test_resolve_skips_source_with_no_url():
    sources = [
        {"name": "Broken"},
        {
            "name": "NWS Climatological Report",
            "url": "https://forecast.weather.gov/product.php?product=CLI&issuedby=DCA",
        },
    ]
    resolved = resolve_settlement_station("KXHIGHDC", sources)
    assert resolved is not None
    assert resolved["station_id"] == "DCA"


# ---------------------------------------------------------------------------
# station_coords.resolve_station_coordinates — cache-first station→(lat, lon)
# ---------------------------------------------------------------------------

class FakeStationClient:
    """Stand-in NOAAClient that returns canned station coordinates and counts calls."""

    def __init__(self, coordinates_by_station: dict[str, tuple[float, float] | None]) -> None:
        self._coordinates = coordinates_by_station
        self.calls: list[str] = []

    async def get_station_coordinates(self, station_id: str) -> tuple[float, float] | None:
        self.calls.append(station_id)
        return self._coordinates.get(station_id)


_LAX_TERMS = {
    "KXHIGHLAX": {
        "settlement_sources": [
            {"name": "NWS Climatological Report",
             "url": "https://forecast.weather.gov/product.php?product=CLI&issuedby=LAX"}
        ]
    }
}


def test_resolve_station_coordinates_resolves_and_caches(tmp_path, monkeypatch):
    path = tmp_path / "series_settlement_stations.json"
    monkeypatch.setattr(station_coords, "load_contract_terms", lambda *a, **k: _LAX_TERMS)
    client = FakeStationClient({"LAX": (33.9382, -118.3866)})

    async def scenario():
        first = await station_coords.resolve_station_coordinates(
            "KXHIGHLAX-26JUN04-T85", client, path=path
        )
        # Second lookup (bare series) is a cache hit — no second NOAA call.
        second = await station_coords.resolve_station_coordinates("KXHIGHLAX", client, path=path)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == (33.9382, -118.3866)
    assert second == first
    assert client.calls == ["LAX"]  # resolved once, then cached
    cached = station_coords.load_station_coordinates(path)
    assert cached["KXHIGHLAX"]["station_id"] == "LAX"
    assert station_coords.station_label_for_series("KXHIGHLAX-26JUN04-T85", path) == "station:LAX"


def test_resolve_station_coordinates_accuweather_is_none_and_cached(tmp_path, monkeypatch):
    path = tmp_path / "series_settlement_stations.json"
    terms = {"KXTEMPNYCH": {"settlement_sources": [
        {"name": "AccuWeather", "url": "https://www.accuweather.com"}]}}
    monkeypatch.setattr(station_coords, "load_contract_terms", lambda *a, **k: terms)
    client = FakeStationClient({})

    result = asyncio.run(
        station_coords.resolve_station_coordinates("KXTEMPNYCH", client, path=path)
    )
    assert result is None
    assert client.calls == []  # recognized non-station source — NOAA never queried
    cached = station_coords.load_station_coordinates(path)
    assert cached["KXTEMPNYCH"]["station_id"] is None  # negative cached
    assert station_coords.station_label_for_series("KXTEMPNYCH", path) == "centroid"


def test_resolve_station_coordinates_no_terms_returns_none_without_caching(tmp_path, monkeypatch):
    path = tmp_path / "series_settlement_stations.json"
    monkeypatch.setattr(station_coords, "load_contract_terms", lambda *a, **k: {})
    client = FakeStationClient({})

    result = asyncio.run(
        station_coords.resolve_station_coordinates("KXHIGHLAX", client, path=path)
    )
    assert result is None
    # Terms may arrive on a later cycle, so nothing is cached.
    assert station_coords.load_station_coordinates(path) == {}


def test_resolve_station_coordinates_geometry_failure_not_cached(tmp_path, monkeypatch):
    path = tmp_path / "series_settlement_stations.json"
    monkeypatch.setattr(station_coords, "load_contract_terms", lambda *a, **k: _LAX_TERMS)
    client = FakeStationClient({})  # LAX missing → get_station_coordinates returns None

    result = asyncio.run(
        station_coords.resolve_station_coordinates("KXHIGHLAX", client, path=path)
    )
    assert result is None
    assert client.calls == ["LAX"]
    # Geometry fetch failed — don't cache, so a later cycle can retry.
    assert station_coords.load_station_coordinates(path) == {}
