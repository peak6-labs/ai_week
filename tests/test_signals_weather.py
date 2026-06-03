"""Tests for kalshi_trader/signals/weather.py"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from kalshi_trader.signals.weather import build_weather_signal


# ---------------------------------------------------------------------------
# Precipitation tests
# ---------------------------------------------------------------------------

def test_build_weather_signal_precipitation_basic():
    forecast = {"temp_high": 80, "temp_low": 65, "precip_pct": 73, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-RAIN-JUNE3",
        metric="precipitation",
        threshold=0.0,
        operator="above",
        forecast=forecast,
    )
    assert sig.source == "noaa_gfs"
    assert sig.probability == pytest.approx(0.73)
    assert sig.uncertainty == pytest.approx(0.05)
    assert sig.weight == pytest.approx(0.85)
    assert sig.metadata["data_quality"] == "fresh"
    assert sig.metadata["ticker"] == "WEATHER-NYC-RAIN-JUNE3"
    assert sig.metadata["forecast_model"] == "noaa_gfs"
    assert isinstance(sig.metadata["narrative"], str)
    assert len(sig.metadata["narrative"]) > 0


def test_build_weather_signal_precipitation_stale():
    forecast = {"temp_high": 80, "temp_low": 65, "precip_pct": 50, "data_age_minutes": 120}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-RAIN-JUNE3",
        metric="precipitation",
        threshold=0.0,
        operator="above",
        forecast=forecast,
    )
    assert sig.metadata["data_quality"] == "stale"


def test_build_weather_signal_precipitation_unavailable():
    forecast = {"temp_high": 80, "temp_low": 65, "precip_pct": 50, "data_age_minutes": 400}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-RAIN-JUNE3",
        metric="precipitation",
        threshold=0.0,
        operator="above",
        forecast=forecast,
    )
    assert sig.metadata["data_quality"] == "unavailable"


# ---------------------------------------------------------------------------
# Temperature tests
# ---------------------------------------------------------------------------

def test_build_weather_signal_temp_above():
    # mean=(90+70)/2=80, std=(90-70)/4=5 → P(X>85) = sf(85) ≈ 0.159
    # But we want prob > 0.5 so use threshold=75 (below mean) → P(X>75) > 0.5
    forecast = {"temp_high": 90, "temp_low": 70, "precip_pct": 0, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-TEMP-JUNE3",
        metric="temp_high",
        threshold=75.0,
        operator="above",
        forecast=forecast,
    )
    assert sig.probability > 0.5


def test_build_weather_signal_temp_below():
    # mean=(60+50)/2=55, std=(60-50)/4=2.5 → P(X<75) >> 0.5
    forecast = {"temp_high": 60, "temp_low": 50, "precip_pct": 0, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-TEMP-JUNE3",
        metric="temp_low",
        threshold=75.0,
        operator="below",
        forecast=forecast,
    )
    assert sig.probability > 0.5


def test_build_weather_signal_temp_high_below_threshold_is_low_prob():
    # Regression: forecast HIGH is 83°F, threshold <79°F. P(high < 79) must be
    # LOW — the high is above the threshold. The old code centered on the
    # daily-average midpoint (72°F) and returned ~0.90, badly wrong.
    forecast = {"temp_high": 83, "temp_low": 61, "precip_pct": 0, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="KXHIGHTDC-26JUN03-T79", metric="temp_high",
        threshold=79.0, operator="below", forecast=forecast,
    )
    assert sig.probability < 0.35


def test_build_weather_signal_temp_high_above_threshold_is_high_prob():
    forecast = {"temp_high": 83, "temp_low": 61, "precip_pct": 0, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="KXHIGHTDC-26JUN03-T79", metric="temp_high",
        threshold=79.0, operator="above", forecast=forecast,
    )
    assert sig.probability > 0.65


def test_build_weather_signal_temp_low_centered_on_low_not_midpoint():
    # Forecast LOW is 60°F; P(low < 45) must be near-zero (low is well above 45).
    forecast = {"temp_high": 82, "temp_low": 60, "precip_pct": 0, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="KXLOWTBOS-26JUN03-T45", metric="temp_low",
        threshold=45.0, operator="below", forecast=forecast,
    )
    assert sig.probability < 0.10


def test_build_weather_signal_temp_uncertainty():
    forecast = {"temp_high": 90, "temp_low": 70, "precip_pct": 0, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-TEMP-JUNE3",
        metric="temp_high",
        threshold=85.0,
        operator="above",
        forecast=forecast,
    )
    assert sig.uncertainty == pytest.approx(0.08)


# ---------------------------------------------------------------------------
# Discussion / qualitative fields
# ---------------------------------------------------------------------------

def test_build_weather_signal_with_discussion():
    forecast = {"temp_high": 80, "temp_low": 65, "precip_pct": 60, "data_age_minutes": 30}
    discussion = {"confidence": "high", "key_points": ["Storm timing uncertain."]}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-RAIN-JUNE3",
        metric="precipitation",
        threshold=0.0,
        operator="above",
        forecast=forecast,
        discussion=discussion,
    )
    assert sig.metadata.get("nws_confidence") == "high"
    assert sig.metadata.get("key_uncertainty") == "Storm timing uncertain."


def test_build_weather_signal_discussion_no_key_points():
    forecast = {"temp_high": 80, "temp_low": 65, "precip_pct": 60, "data_age_minutes": 30}
    discussion = {"confidence": "medium", "key_points": []}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-RAIN-JUNE3",
        metric="precipitation",
        threshold=0.0,
        operator="above",
        forecast=forecast,
        discussion=discussion,
    )
    assert "key_uncertainty" not in sig.metadata
    assert sig.metadata.get("nws_confidence") == "medium"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_build_weather_signal_probability_clamped():
    # precip_pct=0 → raw prob=0.0, must clamp to 0.01
    forecast = {"temp_high": 80, "temp_low": 65, "precip_pct": 0, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-RAIN-JUNE3",
        metric="precipitation",
        threshold=0.0,
        operator="above",
        forecast=forecast,
    )
    assert sig.probability >= 0.01


def test_build_weather_signal_probability_clamped_high():
    # precip_pct=100 → raw prob=1.0, must clamp to 0.99
    forecast = {"temp_high": 80, "temp_low": 65, "precip_pct": 100, "data_age_minutes": 30}
    sig = build_weather_signal(
        ticker="WEATHER-NYC-RAIN-JUNE3",
        metric="precipitation",
        threshold=0.0,
        operator="above",
        forecast=forecast,
    )
    assert sig.probability <= 0.99


def test_build_weather_signal_issued_at_reflects_age():
    age_minutes = 60
    forecast = {"temp_high": 80, "temp_low": 65, "precip_pct": 50, "data_age_minutes": age_minutes}
    before = datetime.now(tz=timezone.utc)
    sig = build_weather_signal(
        ticker="WEATHER-NYC-RAIN-JUNE3",
        metric="precipitation",
        threshold=0.0,
        operator="above",
        forecast=forecast,
    )
    after = datetime.now(tz=timezone.utc)
    expected_low = before - timedelta(minutes=age_minutes) - timedelta(seconds=5)
    expected_high = after - timedelta(minutes=age_minutes) + timedelta(seconds=5)
    assert sig.data_issued_at.tzinfo is not None
    assert expected_low <= sig.data_issued_at <= expected_high
