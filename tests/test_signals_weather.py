"""Tests for kalshi_trader/signals/weather.py"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from kalshi_trader.signals.weather import (
    build_authority_signal,
    build_ensemble_signal,
    build_weather_signal,
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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


# ---------------------------------------------------------------------------
# build_authority_signal (X meteorologist-authority second source)
# ---------------------------------------------------------------------------

def test_build_authority_signal_source_weight_uncertainty():
    forecast = {
        "temp_high": 88, "temp_low": 71, "precip_pct": 20,
        "confidence": "high", "post_count": 2, "issued_at": _now_iso(),
        "key_quotes": ["High near 88 Friday."], "handles": ["wfaaweather"],
    }
    sig = build_authority_signal(
        ticker="KXHIGHTDAL-26JUN05-T85", metric="temp_high", threshold=85.0,
        operator="above", authority_forecast=forecast, independent_of_noaa=True,
    )
    assert sig.source == "x_weather_authority"
    assert sig.weight == pytest.approx(0.70)
    # base temp uncertainty, high confidence, 2 posts, independent → no bumps
    assert sig.uncertainty == pytest.approx(0.10)
    assert sig.metadata["forecast_model"] == "x_weather_authority"
    assert sig.metadata["independent_of_noaa"] is True
    assert sig.metadata["post_count"] == 2
    assert sig.metadata["handles"] == ["wfaaweather"]
    assert sig.metadata["forecast_high"] == 88
    assert sig.metadata["key_quotes"] == ["High near 88 Friday."]


def test_build_authority_signal_reuses_noaa_math():
    # Same forecast values fed to both builders must yield the same probability —
    # confirms the shared _metric_to_probability calibration path.
    noaa = build_weather_signal(
        ticker="KXHIGHTDC-26JUN03-T79", metric="temp_high", threshold=79.0,
        operator="above",
        forecast={"temp_high": 83, "temp_low": 61, "precip_pct": 0, "data_age_minutes": 0},
    )
    authority_forecast = {
        "temp_high": 83, "temp_low": 61, "precip_pct": 0,
        "confidence": "high", "post_count": 2, "issued_at": _now_iso(),
        "handles": ["capitalweather"],
    }
    authority = build_authority_signal(
        ticker="KXHIGHTDC-26JUN03-T79", metric="temp_high", threshold=79.0,
        operator="above", authority_forecast=authority_forecast, independent_of_noaa=True,
    )
    assert authority.probability == pytest.approx(noaa.probability)


def test_build_authority_signal_precipitation_probability_and_uncertainty():
    forecast = {
        "temp_high": None, "temp_low": None, "precip_pct": 60,
        "confidence": "high", "post_count": 2, "issued_at": _now_iso(), "handles": ["mattlanza"],
    }
    sig = build_authority_signal(
        ticker="KXRAINHOU-26JUN05", metric="precipitation", threshold=0.0,
        operator="above", authority_forecast=forecast, independent_of_noaa=True,
    )
    assert sig.probability == pytest.approx(0.60)
    assert sig.uncertainty == pytest.approx(0.07)  # base precip uncertainty


def test_build_authority_signal_zero_posts_is_empty():
    forecast = {
        "temp_high": None, "temp_low": None, "precip_pct": None,
        "confidence": "low", "post_count": 0, "issued_at": _now_iso(), "handles": [],
    }
    sig = build_authority_signal(
        ticker="KXHIGHTDAL-26JUN05-T85", metric="temp_high", threshold=85.0,
        operator="above", authority_forecast=forecast, independent_of_noaa=True,
    )
    assert sig.uncertainty == 1.0
    assert sig.metadata["data_quality"] == "empty"


def test_build_authority_signal_missing_needed_metric_is_empty():
    # Posts exist (post_count > 0) but the needed metric value is null → empty.
    forecast = {
        "temp_high": None, "temp_low": 60, "precip_pct": 30,
        "confidence": "high", "post_count": 3, "issued_at": _now_iso(), "handles": ["wfaaweather"],
    }
    sig = build_authority_signal(
        ticker="KXHIGHTDAL-26JUN05-T85", metric="temp_high", threshold=85.0,
        operator="above", authority_forecast=forecast, independent_of_noaa=True,
    )
    assert sig.uncertainty == 1.0
    assert sig.metadata["data_quality"] == "empty"


def test_build_authority_signal_non_independent_raises_uncertainty():
    base_forecast = {
        "temp_high": 88, "temp_low": 71, "precip_pct": 0,
        "confidence": "high", "post_count": 2, "issued_at": _now_iso(),
    }
    independent = build_authority_signal(
        ticker="T", metric="temp_high", threshold=85.0, operator="above",
        authority_forecast={**base_forecast, "handles": ["wfaaweather"]},
        independent_of_noaa=True,
    )
    non_independent = build_authority_signal(
        ticker="T", metric="temp_high", threshold=85.0, operator="above",
        authority_forecast={**base_forecast, "handles": ["NWSBoston"]},
        independent_of_noaa=False,
    )
    assert non_independent.uncertainty > independent.uncertainty
    assert non_independent.uncertainty == pytest.approx(0.15)  # 0.10 + 0.05 bump
    assert non_independent.metadata["independent_of_noaa"] is False


def test_build_authority_signal_low_confidence_bumps_uncertainty():
    forecast = {
        "temp_high": 88, "temp_low": 71, "precip_pct": 0,
        "confidence": "low", "post_count": 2, "issued_at": _now_iso(), "handles": ["wfaaweather"],
    }
    sig = build_authority_signal(
        ticker="T", metric="temp_high", threshold=85.0, operator="above",
        authority_forecast=forecast, independent_of_noaa=True,
    )
    assert sig.uncertainty == pytest.approx(0.15)


def test_build_authority_signal_single_post_bumps_uncertainty():
    forecast = {
        "temp_high": 88, "temp_low": 71, "precip_pct": 0,
        "confidence": "high", "post_count": 1, "issued_at": _now_iso(), "handles": ["wfaaweather"],
    }
    sig = build_authority_signal(
        ticker="T", metric="temp_high", threshold=85.0, operator="above",
        authority_forecast=forecast, independent_of_noaa=True,
    )
    assert sig.uncertainty == pytest.approx(0.15)


def test_build_authority_signal_issued_at_reflects_post_age():
    issued_at = (datetime.now(tz=timezone.utc) - timedelta(minutes=120)).isoformat()
    forecast = {
        "temp_high": 88, "temp_low": 71, "precip_pct": 0,
        "confidence": "high", "post_count": 2, "issued_at": issued_at, "handles": ["wfaaweather"],
    }
    sig = build_authority_signal(
        ticker="T", metric="temp_high", threshold=85.0, operator="above",
        authority_forecast=forecast, independent_of_noaa=True,
    )
    assert sig.data_issued_at.tzinfo is not None
    age_minutes = (datetime.now(tz=timezone.utc) - sig.data_issued_at).total_seconds() / 60.0
    assert 119 <= age_minutes <= 121
    assert sig.metadata["data_quality"] == "stale"  # 120 min → stale band


# ---------------------------------------------------------------------------
# build_ensemble_signal (GEFS ensemble empirical-CDF — primary weather signal)
# ---------------------------------------------------------------------------

def _ensemble(members: list, field: str = "temperature_2m_max", units: str = "°F") -> dict:
    return {
        "members": members,
        "member_count": len(members),
        "field": field,
        "units": units,
        "model": "gfs_seamless",
        "data_issued_at": datetime.now(tz=timezone.utc),
    }


def test_build_ensemble_signal_temp_above_is_member_fraction():
    members = [70.0 + index for index in range(31)]  # 70..100
    sig = build_ensemble_signal(
        ticker="KXHIGHTCHI-26JUN05-T85", metric="temp_high", threshold=85.0,
        operator="above", ensemble=_ensemble(members),
    )
    expected_satisfying = sum(1 for value in members if value > 85.0)
    assert sig.source == "gfs_ensemble"
    assert sig.probability == pytest.approx(expected_satisfying / len(members))
    assert sig.metadata["member_count"] == 31
    assert sig.metadata["members_satisfying"] == expected_satisfying
    assert sig.metadata["forecast_model"] == "gfs_ensemble"


def test_build_ensemble_signal_temp_below_is_member_fraction():
    members = [40.0 + index for index in range(31)]  # 40..70
    sig = build_ensemble_signal(
        ticker="KXLOWTCHI-26JUN05-T50", metric="temp_low", threshold=50.0,
        operator="below", ensemble=_ensemble(members, field="temperature_2m_min"),
    )
    expected_satisfying = sum(1 for value in members if value < 50.0)
    assert sig.probability == pytest.approx(expected_satisfying / len(members))


def test_build_ensemble_signal_precipitation_uses_measurable_threshold():
    # threshold 0 → count members exceeding 0.01" (measurable precip).
    members = [0.0] * 20 + [0.05] * 11
    sig = build_ensemble_signal(
        ticker="KXRAINCHI-26JUN05", metric="precipitation", threshold=0.0,
        operator="above", ensemble=_ensemble(members, field="precipitation_sum", units="inch"),
    )
    assert sig.probability == pytest.approx(11 / 31)
    assert sig.uncertainty == pytest.approx(0.05)  # precip base uncertainty


def test_build_ensemble_signal_precipitation_explicit_threshold():
    members = [0.2] * 10 + [0.8] * 21  # 21 members exceed 0.5"
    sig = build_ensemble_signal(
        ticker="KXRAINCHI-26JUN05-T0.5", metric="precipitation", threshold=0.5,
        operator="above", ensemble=_ensemble(members, field="precipitation_sum", units="inch"),
    )
    assert sig.probability == pytest.approx(21 / 31)


def test_build_ensemble_signal_probability_clamped_high():
    members = [90.0] * 31  # all above threshold → raw 1.0 → clamp 0.99
    sig = build_ensemble_signal(
        ticker="T", metric="temp_high", threshold=50.0, operator="above",
        ensemble=_ensemble(members),
    )
    assert sig.probability == pytest.approx(0.99)


def test_build_ensemble_signal_probability_clamped_low():
    members = [40.0] * 31  # none above threshold → raw 0.0 → clamp 0.01
    sig = build_ensemble_signal(
        ticker="T", metric="temp_high", threshold=50.0, operator="above",
        ensemble=_ensemble(members),
    )
    assert sig.probability == pytest.approx(0.01)


def test_build_ensemble_signal_too_few_members_is_empty():
    members = [80.0] * 5  # below ensemble_min_members (10)
    sig = build_ensemble_signal(
        ticker="T", metric="temp_high", threshold=75.0, operator="above",
        ensemble=_ensemble(members),
    )
    assert sig.source == "gfs_ensemble"
    assert sig.uncertainty == 1.0
    assert sig.metadata["data_quality"] == "empty"
    assert sig.metadata["member_count"] == 5


def test_build_ensemble_signal_source_weight_uncertainty_metadata():
    members = [70.0 + index for index in range(31)]
    sig = build_ensemble_signal(
        ticker="T", metric="temp_high", threshold=85.0, operator="above",
        ensemble=_ensemble(members),
    )
    assert sig.source == "gfs_ensemble"
    assert sig.weight == pytest.approx(0.85)  # weight_ensemble default
    assert sig.uncertainty == pytest.approx(0.07)  # uncertainty_ensemble_temp default
    assert sig.metadata["data_quality"] == "fresh"
    assert "ensemble_mean" in sig.metadata
    assert "ensemble_median" in sig.metadata
    assert "percentile_10" in sig.metadata
    assert "percentile_90" in sig.metadata
