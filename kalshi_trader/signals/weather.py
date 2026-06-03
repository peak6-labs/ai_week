"""Converter: raw NOAA forecast data → SignalEstimate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import scipy.stats

from kalshi_trader.models import SignalEstimate
from kalshi_trader.ui.config_manager import cfg


def build_weather_signal(
    ticker: str,
    metric: str,
    threshold: float,
    operator: str,
    forecast: dict,
    discussion: dict | None = None,
) -> SignalEstimate:
    """Build a SignalEstimate from NOAA forecast data.

    Args:
        ticker: Kalshi market ticker.
        metric: "temp_high", "temp_low", or "precipitation".
        threshold: Numeric threshold for the condition.
        operator: "above" or "below".
        forecast: Dict with temp_high, temp_low, precip_pct, data_age_minutes.
        discussion: Optional dict with confidence and key_points list.

    Returns:
        SignalEstimate with source="noaa_gfs".
    """
    data_age_minutes: float = forecast.get("data_age_minutes", 0) or 0

    # Determine probability and uncertainty
    if metric in ("temp_high", "temp_low"):
        high = forecast["temp_high"] if forecast.get("temp_high") is not None else 85.0
        low = forecast["temp_low"] if forecast.get("temp_low") is not None else 65.0
        # Center the distribution on the metric actually being traded — the daily
        # HIGH for temp_high markets, the daily LOW for temp_low — NOT the
        # daily-average midpoint (which put temp_high markets ~10°F below the
        # forecast high and produced backwards probabilities). std is a
        # forecast-error proxy from the diurnal range; its exact calibration is
        # left to the paper-trade loop (#25).
        mean = high if metric == "temp_high" else low
        std = max((high - low) / 6.0, 2.0)
        dist = scipy.stats.norm(mean, std)
        raw_prob = float(dist.sf(threshold) if operator == "above" else dist.cdf(threshold))
        uncertainty = cfg.get("uncertainty_noaa_temp")
    elif metric == "precipitation":
        raw_prob = forecast.get("precip_pct", 0) / 100.0
        uncertainty = cfg.get("uncertainty_noaa_precip")
    else:
        raise ValueError(f"Unsupported metric: {metric}")

    probability = min(max(raw_prob, 0.01), 0.99)

    # data_issued_at: now minus data age, timezone-aware UTC
    data_issued_at = datetime.now(tz=timezone.utc) - timedelta(minutes=data_age_minutes)

    # data_quality label
    if data_age_minutes < 60:
        data_quality = "fresh"
    elif data_age_minutes < 360:
        data_quality = "stale"
    else:
        data_quality = "unavailable"

    # Build 1-2 sentence narrative
    if metric == "precipitation":
        narrative = (
            f"NOAA GFS shows {forecast.get('precip_pct', 0)}% precipitation probability "
            f"for {ticker}. Data is {data_quality} ({data_age_minutes:.0f} min old)."
        )
    else:
        high = forecast.get("temp_high", "?")
        low = forecast.get("temp_low", "?")
        narrative = (
            f"NOAA GFS forecast high {high}°F / low {low}°F for {ticker}. "
            f"P({metric} {operator} {threshold}) = {probability:.2%}. Data is {data_quality}."
        )

    metadata: dict = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": data_quality,
        "forecast_model": "noaa_gfs",
    }

    if discussion is not None:
        metadata["nws_confidence"] = discussion.get("confidence")
        key_points = discussion.get("key_points") or []
        if key_points:
            metadata["key_uncertainty"] = key_points[0]

    return SignalEstimate(
        source="noaa_gfs",
        probability=probability,
        uncertainty=uncertainty,
        weight=cfg.get("weight_noaa"),
        data_issued_at=data_issued_at,
        metadata=metadata,
    )
