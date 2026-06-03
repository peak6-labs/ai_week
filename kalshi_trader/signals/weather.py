"""Converter: raw NOAA forecast data → SignalEstimate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import scipy.stats

from kalshi_trader.models import SignalEstimate
from kalshi_trader.ui.config_manager import cfg


# Climatological local hour by which the daily temperature extreme is typically
# set (lows bottom out near sunrise, highs peak mid/late afternoon) and the hours
# of ramp-up before it. Used to gauge how "locked" a same-day extreme is.
_EXTREME_LOCK_HOUR = {"temp_low": 9.0, "temp_high": 17.0}
_LOCK_RAMP_HOURS = 3.0


def observation_lock_fraction(
    metric: str, observation: dict | None, now: datetime | None = None
) -> float:
    """How locked the day's realized extreme is, in [0, 1].

    Drives how far the clamped-ensemble uncertainty collapses toward the floor.
    Returns 0 early in the day (model still leads) and ramps to 1 once the latest
    observation is past the metric's climatological extreme hour. For
    precipitation (monotonic accumulation that can only grow) it ramps with the
    fraction of the local day elapsed. Returns 0 when the observation lacks a
    usable timezone/timestamp.
    """
    if not observation:
        return 0.0
    timezone_name = observation.get("timezone")
    latest_timestamp = observation.get("latest_timestamp") or observation.get("at_timestamp")
    if not timezone_name or not latest_timestamp:
        return 0.0
    try:
        latest = datetime.fromisoformat(str(latest_timestamp).replace("Z", "+00:00"))
        local_now = (now or latest).astimezone(ZoneInfo(timezone_name))
    except (ValueError, TypeError, KeyError):
        return 0.0

    local_hour = local_now.hour + local_now.minute / 60.0
    if metric == "precipitation":
        return min(max(local_hour / 24.0, 0.0), 1.0)

    lock_hour = _EXTREME_LOCK_HOUR.get(metric)
    if lock_hour is None:
        return 0.0
    ramp_start = lock_hour - _LOCK_RAMP_HOURS
    return min(max((local_hour - ramp_start) / _LOCK_RAMP_HOURS, 0.0), 1.0)


def _metric_to_probability(
    metric: str,
    threshold: float,
    operator: str,
    temp_high: float | None,
    temp_low: float | None,
    precip_pct: float | None,
    threshold_high: float | None = None,
) -> float:
    """Raw (unclamped) probability for a weather condition from forecast values.

    The single calibration path shared by the NOAA and authority signal builders:
    a normal centered on the traded metric (the daily HIGH for ``temp_high``, the
    daily LOW for ``temp_low``) with a diurnal-range std proxy; precipitation is
    the percent probability directly. Callers clamp the result to [0.01, 0.99].

    A band market (``operator == "between"``) settles on the closed interval
    ``[threshold, threshold_high]``; its probability is ``CDF(high) - CDF(low)``.
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
        if operator == "between":
            band_high = threshold_high if threshold_high is not None else threshold
            return float(dist.cdf(band_high) - dist.cdf(threshold))
        return float(dist.sf(threshold) if operator == "above" else dist.cdf(threshold))
    if metric == "precipitation":
        return (precip_pct if precip_pct is not None else 0) / 100.0
    raise ValueError(f"Unsupported metric: {metric}")


def _condition_text(
    metric: str, operator: str, threshold: float, threshold_high: float | None
) -> str:
    """Human-readable condition for narratives, e.g. ``temp_high above 85`` or,
    for a band, ``85 ≤ temp_high ≤ 86``."""
    if operator == "between" and threshold_high is not None:
        return f"{threshold} ≤ {metric} ≤ {threshold_high}"
    return f"{metric} {operator} {threshold}"


def build_weather_signal(
    ticker: str,
    metric: str,
    threshold: float,
    operator: str,
    forecast: dict,
    discussion: dict | None = None,
    threshold_high: float | None = None,
) -> SignalEstimate:
    """Build a SignalEstimate from NOAA forecast data.

    Args:
        ticker: Kalshi market ticker.
        metric: "temp_high", "temp_low", or "precipitation".
        threshold: Numeric threshold for the condition (the low edge when
            ``operator == "between"``).
        operator: "above", "below", or "between" (a band/range market).
        forecast: Dict with temp_high, temp_low, precip_pct, data_age_minutes.
        discussion: Optional dict with confidence and key_points list.
        threshold_high: Upper edge of the band when ``operator == "between"``.

    Returns:
        SignalEstimate with source="noaa_gfs".
    """
    data_age_minutes: float = forecast.get("data_age_minutes", 0) or 0

    # Determine probability (shared calibration path) and the metric's uncertainty.
    raw_prob = _metric_to_probability(
        metric, threshold, operator,
        forecast.get("temp_high"), forecast.get("temp_low"), forecast.get("precip_pct"),
        threshold_high,
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
            f"P({_condition_text(metric, operator, threshold, threshold_high)}) "
            f"= {probability:.2%}. Data is {data_quality}."
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


# Standard NWS "measurable precipitation" threshold (inches) — used for rain
# markets that ask "will it rain" with no explicit amount in the threshold.
_MEASURABLE_PRECIP_INCHES = 0.01


def build_ensemble_signal(
    ticker: str,
    metric: str,
    threshold: float,
    operator: str,
    ensemble: dict,
    threshold_high: float | None = None,
    observation: dict | None = None,
    lock_fraction: float = 0.0,
) -> SignalEstimate:
    """Build a ``gfs_ensemble`` SignalEstimate from GEFS member values.

    The model-implied probability is the *empirical CDF* — the fraction of
    ensemble members that satisfy the threshold — which replaces the parametric
    normal-CDF proxy (``build_weather_signal``) as the primary quantitative
    weather signal::

        above   → members strictly greater than threshold
        below   → members strictly less than threshold
        between → members within the closed band [threshold, threshold_high]

    For precipitation with ``threshold <= 0`` (a "will it rain" market), a member
    "satisfies" when its forecast accumulation exceeds 0.01" (measurable precip).

    **Live-observation override:** when ``observation`` carries a realized extreme
    (from ``NOAAClient.get_observed_extreme``) for a same-day market, each member
    is clamped to respect what has already happened — a member forecast the
    observation has falsified is moved to the realized bound, by monotonicity::

        temp_low      → final low ≤ realized min  ⇒ member = min(member, realized)
        temp_high     → final high ≥ realized max ⇒ member = max(member, realized)
        precipitation → final total ≥ realized    ⇒ member = max(member, realized)

    The empirical CDF is then recomputed on the clamped members, and the
    estimate's uncertainty collapses toward ``observation_uncertainty_floor`` in
    proportion to ``lock_fraction`` (how far past the climatological extreme time
    we are). With no observation the function is byte-for-byte the pure ensemble.

    When fewer than ``ensemble_min_members`` are present the estimate is flagged
    ``data_quality == "empty"`` with ``uncertainty = 1.0`` so the scorer drops it
    and the agent's parametric NOAA fallback stands instead.

    Args:
        ticker: Kalshi market ticker.
        metric: "temp_high", "temp_low", or "precipitation".
        threshold: Numeric threshold for the condition (the low edge when
            ``operator == "between"``).
        operator: "above", "below", or "between" (a band/range market).
        ensemble: Dict from ``OpenMeteoClient.get_ensemble_members`` (members,
            member_count, field, units, model, data_issued_at).
        threshold_high: Upper edge of the band when ``operator == "between"``.
        observation: Optional dict from ``NOAAClient.get_observed_extreme`` whose
            ``realized_extreme`` clamps the members (None / absent → no override).
        lock_fraction: How locked the realized extreme is, in [0, 1] (see
            ``observation_lock_fraction``); shrinks uncertainty when clamping.

    Returns:
        SignalEstimate with source="gfs_ensemble".
    """
    members: list[float] = [float(value) for value in ensemble.get("members", [])]
    member_count = len(members)
    minimum_members = int(cfg.get("ensemble_min_members"))

    if member_count < minimum_members:
        # Not enough ensemble members — flag empty so the scorer drops it and the
        # caller's parametric NOAA estimate carries the market instead.
        return SignalEstimate(
            source="gfs_ensemble",
            probability=0.5,
            uncertainty=1.0,
            weight=cfg.get("weight_ensemble"),
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": (
                    f"GEFS ensemble unavailable for {ticker} "
                    f"({member_count} members); falling back to NOAA parametric."
                ),
                "data_quality": "empty",
                "forecast_model": "gfs_ensemble",
                "member_count": member_count,
            },
        )

    # Live-observation override: clamp members to respect the realized extreme
    # (monotonicity) before computing the empirical CDF. Harmless early in the day
    # — the bound only bites once the obs approaches the band.
    realized_extreme = (observation or {}).get("realized_extreme")
    members_clamped = False
    if realized_extreme is not None:
        realized_extreme = float(realized_extreme)
        if metric == "temp_low":
            members = [min(member, realized_extreme) for member in members]
        else:  # temp_high / precipitation: the realized value is a LOWER bound
            members = [max(member, realized_extreme) for member in members]
        members_clamped = True

    if metric == "precipitation":
        effective_threshold = threshold if threshold and threshold > 0 else _MEASURABLE_PRECIP_INCHES
        members_satisfying = sum(1 for value in members if value > effective_threshold)
    elif operator == "between":
        band_high = threshold_high if threshold_high is not None else threshold
        members_satisfying = sum(1 for value in members if threshold <= value <= band_high)
    elif operator == "above":
        members_satisfying = sum(1 for value in members if value > threshold)
    else:  # below
        members_satisfying = sum(1 for value in members if value < threshold)

    raw_probability = members_satisfying / member_count
    probability = min(max(raw_probability, 0.01), 0.99)

    sorted_members = sorted(members)
    ensemble_mean = sum(members) / member_count
    ensemble_median = sorted_members[member_count // 2]
    percentile_10 = sorted_members[max(0, int(0.10 * (member_count - 1)))]
    percentile_90 = sorted_members[min(member_count - 1, int(0.90 * (member_count - 1)))]

    uncertainty = (
        cfg.get("uncertainty_ensemble_precip")
        if metric == "precipitation"
        else cfg.get("uncertainty_ensemble_temp")
    )
    # When clamped to a realized observation, shrink uncertainty toward the floor
    # in proportion to how locked the extreme is.
    bounded_lock_fraction = min(max(lock_fraction, 0.0), 1.0)
    if members_clamped:
        uncertainty_floor = cfg.get("observation_uncertainty_floor")
        uncertainty = uncertainty * (1.0 - bounded_lock_fraction) + uncertainty_floor * bounded_lock_fraction

    if operator == "between":
        band_high = threshold_high if threshold_high is not None else threshold
        condition_text = f"in [{threshold}, {band_high}]"
    else:
        condition_text = f"{operator} {threshold}"
    narrative = (
        f"GEFS {member_count}-member ensemble: {members_satisfying}/{member_count} members "
        f"{condition_text} for {ticker}. P = {probability:.2%}. "
        f"Ensemble median {ensemble_median:.1f}, p10–p90 "
        f"[{percentile_10:.1f}, {percentile_90:.1f}]."
    )
    if members_clamped:
        narrative += (
            f" Clamped to realized {metric.replace('temp_', '').replace('_', ' ')} "
            f"{realized_extreme:.1f} observed at "
            f"{(observation or {}).get('station_id', '?')} "
            f"(lock {bounded_lock_fraction:.0%})."
        )

    return SignalEstimate(
        source="gfs_ensemble",
        probability=probability,
        uncertainty=uncertainty,
        weight=cfg.get("weight_ensemble"),
        # Stamp the build time (≈ fetch time, same cycle). We deliberately do NOT
        # read back ensemble["data_issued_at"]: the GEFS forecast is fresh each
        # cycle (see plan — data_issued_at = now), and when this dict round-trips
        # through the agent it is JSON-encoded with default=str, so that field
        # arrives as a string, not a datetime.
        data_issued_at=datetime.now(tz=timezone.utc),
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "forecast_model": "gfs_ensemble",
            "member_count": member_count,
            "members_satisfying": members_satisfying,
            "ensemble_mean": round(ensemble_mean, 2),
            "ensemble_median": round(ensemble_median, 2),
            "percentile_10": round(percentile_10, 2),
            "percentile_90": round(percentile_90, 2),
            "members_clamped": members_clamped,
            "realized_extreme": realized_extreme if members_clamped else None,
            "observation_station": (observation or {}).get("station_id") if members_clamped else None,
            "observation_at": (observation or {}).get("at_timestamp") if members_clamped else None,
            "lock_fraction": round(bounded_lock_fraction, 3) if members_clamped else None,
        },
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
    threshold_high: float | None = None,
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
        threshold: Numeric threshold for the condition (the low edge when
            ``operator == "between"``).
        operator: "above", "below", or "between" (a band/range market).
        authority_forecast: Dict from ``XClient.forecast_search`` (temp_high,
            temp_low, precip_pct, confidence, post_count, issued_at, key_quotes),
            optionally augmented with ``handles``.
        independent_of_noaa: False when every polled handle is an NWS office.
        threshold_high: Upper edge of the band when ``operator == "between"``.

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
            metric, threshold, operator, temp_high, temp_low, precip_pct, threshold_high
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
            f"low {temp_low}°F for {ticker}. "
            f"P({_condition_text(metric, operator, threshold, threshold_high)}) = "
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
