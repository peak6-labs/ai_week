"""Tests for kalshi_trader/external/weather_settlement.py"""
from __future__ import annotations

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
