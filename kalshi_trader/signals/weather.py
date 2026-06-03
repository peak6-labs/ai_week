"""Converter: raw NOAA forecast data → SignalEstimate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import scipy.stats

from kalshi_trader.models import SignalEstimate
from kalshi_trader.ui.config_manager import cfg


def _metric_to_probability(
    metric: str,
    threshold: float,
    operator: str,
    temp_high: float | None,
    temp_low: float | None,
    precip_pct: float | None,
) -> float:
    """Raw (unclamped) probability for a weather condition from forecast values.

    The single calibration path shared by the NOAA and authority signal builders:
    a normal centered on the traded metric (the daily HIGH for ``temp_high``, the
    daily LOW for ``temp_low``) with a diurnal-range std proxy; precipitation is
    the percent probability directly. Callers clamp the result to [0.01, 0.99].
    """
    if metric in ("temp_high", "temp_low"):
        high = temp_high if temp_high is not None else 85.0
        low = temp_low if temp_low is not None else 65.0
        # Center on the metric actually being traded — the daily HIGH for
        # temp_high markets, the daily LOW for temp_low — NOT the daily-average
        # midpoint (which put temp_high markets ~10°F below the forecast high and
        # produced backwards probabilities). std is a forecast-error proxy from
        # the diurnal range; its exact calibration is left to the paper loop (#25).
        mean = high if metric == "temp_high" else low
        std = max((high - low) / 6.0, 2.0)
        dist = scipy.stats.norm(mean, std)
        return float(dist.sf(threshold) if operator == "above" else dist.cdf(threshold))
    if metric == "precipitation":
        return (precip_pct if precip_pct is not None else 0) / 100.0
    raise ValueError(f"Unsupported metric: {metric}")


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

    # Determine probability (shared calibration path) and the metric's uncertainty.
    raw_prob = _metric_to_probability(
        metric, threshold, operator,
        forecast.get("temp_high"), forecast.get("temp_low"), forecast.get("precip_pct"),
    )
    if metric in ("temp_high", "temp_low"):
        uncertainty = cfg.get("uncertainty_noaa_temp")
    elif metric == "precipitation":
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


# Uncertainty bump applied when an authority's post is low-confidence / a lone
# post, and again when the authority is an NWS office (not independent of NOAA).
_AUTHORITY_UNCERTAINTY_BUMP = 0.05


def build_authority_signal(
    ticker: str,
    metric: str,
    threshold: float,
    operator: str,
    authority_forecast: dict,
    independent_of_noaa: bool,
) -> SignalEstimate:
    """Build a SignalEstimate from named-meteorologist X forecasts.

    A *second* weather signal alongside NOAA, sharing the same calibration path
    (``_metric_to_probability``). Source ``x_weather_authority`` — distinct from
    the ``x_grok`` family prefix so it counts as an independent source.

    When the needed metric value is absent or ``post_count == 0`` the estimate is
    flagged ``data_quality == "empty"`` with ``uncertainty = 1.0`` so the scorer
    drops it (falling back to NOAA-only). A non-independent (NWS-office) authority
    carries raised uncertainty and ``independent_of_noaa = False``, which the
    scorer's agreement boost excludes.

    Args:
        ticker: Kalshi market ticker.
        metric: "temp_high", "temp_low", or "precipitation".
        threshold: Numeric threshold for the condition.
        operator: "above" or "below".
        authority_forecast: Dict from ``XClient.forecast_search`` (temp_high,
            temp_low, precip_pct, confidence, post_count, issued_at, key_quotes),
            optionally augmented with ``handles``.
        independent_of_noaa: False when every polled handle is an NWS office.

    Returns:
        SignalEstimate with source="x_weather_authority".
    """
    temp_high = authority_forecast.get("temp_high")
    temp_low = authority_forecast.get("temp_low")
    precip_pct = authority_forecast.get("precip_pct")
    post_count = int(authority_forecast.get("post_count") or 0)
    confidence = authority_forecast.get("confidence") or "low"
    handles = authority_forecast.get("handles") or []
    key_quotes = authority_forecast.get("key_quotes") or []

    needed_value = {
        "temp_high": temp_high,
        "temp_low": temp_low,
        "precipitation": precip_pct,
    }.get(metric)

    # data_issued_at from the most-recent-post timestamp (tz-aware UTC) so the
    # scorer's exp(-age/360) staleness discount applies. Falls back to now.
    issued_at_raw = authority_forecast.get("issued_at")
    data_issued_at = datetime.now(tz=timezone.utc)
    if issued_at_raw:
        try:
            parsed = datetime.fromisoformat(str(issued_at_raw).replace("Z", "+00:00"))
            data_issued_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            data_issued_at = datetime.now(tz=timezone.utc)
    data_age_minutes = max(
        0.0, (datetime.now(tz=timezone.utc) - data_issued_at).total_seconds() / 60.0
    )

    base_uncertainty = (
        cfg.get("uncertainty_x_authority_precip")
        if metric == "precipitation"
        else cfg.get("uncertainty_x_authority_temp")
    )

    if needed_value is None or post_count == 0:
        # No usable forecast — flag empty so the scorer drops it (NOAA-only).
        probability = 0.5
        uncertainty = 1.0
        data_quality = "empty"
        narrative = (
            f"No usable {metric} forecast for {ticker} from the city "
            f"authorities ({', '.join(handles) or 'none'}); falling back to NOAA."
        )
    else:
        raw_prob = _metric_to_probability(
            metric, threshold, operator, temp_high, temp_low, precip_pct
        )
        probability = min(max(raw_prob, 0.01), 0.99)
        uncertainty = base_uncertainty
        # Low-confidence or single-post forecasts are shakier.
        if confidence == "low" or post_count == 1:
            uncertainty += _AUTHORITY_UNCERTAINTY_BUMP
        # An NWS office derives from the NOAA model family — corroboration is
        # circular, so trust it less and (in the scorer) exclude its agreement.
        if independent_of_noaa is False:
            uncertainty += _AUTHORITY_UNCERTAINTY_BUMP
        if data_age_minutes < 60:
            data_quality = "fresh"
        elif data_age_minutes < 360:
            data_quality = "stale"
        else:
            data_quality = "unavailable"
        narrative = (
            f"{len(handles)} authority handle(s) forecast high {temp_high}°F / "
            f"low {temp_low}°F for {ticker}. P({metric} {operator} {threshold}) = "
            f"{probability:.2%}. Confidence {confidence}; "
            f"{'independent' if independent_of_noaa else 'NWS-office (non-independent)'}; "
            f"data is {data_quality}."
        )

    metadata: dict = {
        "ticker": ticker,
        "narrative": narrative,
        "data_quality": data_quality,
        "forecast_model": "x_weather_authority",
        "post_count": post_count,
        "authority_confidence": confidence,
        "handles": handles,
        "independent_of_noaa": independent_of_noaa,
        "forecast_high": temp_high,
        "forecast_low": temp_low,
        "key_quotes": key_quotes,
    }

    return SignalEstimate(
        source="x_weather_authority",
        probability=probability,
        uncertainty=uncertainty,
        weight=cfg.get("weight_x_authority"),
        data_issued_at=data_issued_at,
        metadata=metadata,
    )
