from __future__ import annotations
from datetime import date as date_type, datetime, timezone
from pathlib import Path
from kalshi_trader.models import SignalEstimate
from kalshi_trader.contract_terms import load_contract_terms
from kalshi_trader.external.noaa import NOAAClient
from kalshi_trader.external.open_meteo import OpenMeteoClient
from kalshi_trader.external.weather_parser import parse_title, parse_discussion
from kalshi_trader.external.weather_authorities import get_authorities, is_independent_authority
from kalshi_trader.external.weather_settlement import resolve_settlement_station
from kalshi_trader.external.x_client import XClient
from kalshi_trader.station_coords import resolve_station_coordinates, station_label_for_series
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.weather import (
    build_authority_signal,
    build_ensemble_signal,
    build_weather_signal,
    observation_lock_fraction,
)
from kalshi_trader.ui.config_manager import cfg

_PROMPTS_DIR = Path(__file__).parent / "prompts"


async def _parse_weather_market(ticker: str, title: str) -> dict | None:
    return parse_title(ticker, title)


_SCHEMAS: list[dict] = [
    {
        "name": "parse_weather_market",
        "description": "Parse a Kalshi weather market title into a structured question (city, lat, lon, metric, threshold, operator, target_date). Returns null if unparseable — stop and return [] if null.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["ticker", "title"],
        },
    },
    {
        "name": "get_noaa_forecast",
        "description": "Fetch NWS gridpoint forecast for a lat/lon and date. Returns temp_high, temp_low, precip_pct, wind_mph, short_forecast, data_age_minutes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["lat", "lon", "date"],
        },
    },
    {
        "name": "get_nws_discussion",
        "description": "Fetch and parse the NWS Area Forecast Discussion. Returns confidence ('high'/'medium'/'low') and key_points list. Call this when precip_pct is between 30-70%.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "build_weather_signal",
        "description": "Convert NOAA forecast data into a SignalEstimate dict. Pass the full forecast dict from get_noaa_forecast and optionally the discussion dict from get_nws_discussion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation"]},
                "threshold": {"type": "number", "description": "Condition threshold; the low edge of the band when operator is 'between'."},
                "threshold_high": {"type": "number", "description": "Upper edge of the band; pass only when operator is 'between' (from parse_weather_market's threshold_high)."},
                "operator": {"type": "string", "enum": ["above", "below", "between"]},
                "forecast": {"type": "object"},
                "discussion": {"type": "object"},
            },
            "required": ["ticker", "metric", "threshold", "operator", "forecast"],
        },
    },
    {
        "name": "get_ensemble_forecast",
        "description": "Fetch the 31-member GEFS daily ensemble (Open-Meteo) for a lat/lon, date, and metric. Returns members (list of per-member daily values), member_count, field, units. This is the PRIMARY quantitative source. If member_count is below the minimum (no usable ensemble — e.g. Open-Meteo unreachable or the date is beyond the ~16-day horizon), do NOT call build_ensemble_signal; build the parametric NOAA signal via build_weather_signal instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation"]},
            },
            "required": ["lat", "lon", "date", "metric"],
        },
    },
    {
        "name": "build_ensemble_signal",
        "description": "Convert an ensemble dict (from get_ensemble_forecast) into a SignalEstimate dict whose probability is the fraction of members past the threshold (the empirical CDF). Only call when member_count >= the minimum. Pass the FULL dict returned by get_ensemble_forecast as ensemble, unchanged — do not modify the members array. For a SAME-DAY market, also pass the FULL dict from get_observed_extreme as `observation` (only when its realized_extreme is non-null) so members are clamped to what has already been observed; the lock_fraction rides along inside that dict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation"]},
                "threshold": {"type": "number", "description": "Condition threshold; the low edge of the band when operator is 'between'."},
                "threshold_high": {"type": "number", "description": "Upper edge of the band; pass only when operator is 'between' (from parse_weather_market's threshold_high)."},
                "operator": {"type": "string", "enum": ["above", "below", "between"]},
                "ensemble": {"type": "object"},
                "observation": {"type": "object", "description": "Same-day live observation from get_observed_extreme (full dict). Omit when there is no usable observation (realized_extreme null / station not resolved)."},
            },
            "required": ["ticker", "metric", "threshold", "operator", "ensemble"],
        },
    },
    {
        "name": "get_observed_extreme",
        "description": "SAME-DAY markets only: fetch the realized daily extreme observed SO FAR at the contract's settlement station (resolved from the series' settlement source — never a guessed airport). Returns realized_extreme, station_id, timezone, at_timestamp, lock_fraction, station_resolved. If station_resolved is false or realized_extreme is null there is no usable observation — call build_ensemble_signal WITHOUT an observation. When realized_extreme is present, pass the FULL returned dict as build_ensemble_signal's `observation`. Only call this when target_date is today.",
        "input_schema": {
            "type": "object",
            "properties": {
                "series_ticker": {"type": "string", "description": "The market ticker or its series prefix (e.g. KXLOWTATL or KXLOWTATL-26JUN03-B57.5)."},
                "target_date": {"type": "string", "description": "YYYY-MM-DD; only call when this is today."},
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation"]},
                "station_id": {"type": "string", "description": "Optional NWS station/office code if the settlement context names one; otherwise omit and it is resolved from cached settlement sources."},
            },
            "required": ["series_ticker", "target_date", "metric"],
        },
    },
    {
        "name": "get_authority_forecast",
        "description": "Poll reputable named X/Twitter meteorologist authorities for a city's forecast on a target date. Returns temp_high, temp_low, precip_pct, confidence, post_count, issued_at, handles, independent_of_noaa, data_age_minutes. If post_count is 0 (or no_handles is true) there is no usable authority signal — do NOT call build_authority_signal; emit only the NOAA estimate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Canonical city from parse_weather_market"},
                "target_date": {"type": "string", "description": "YYYY-MM-DD"},
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation"]},
            },
            "required": ["city", "target_date", "metric"],
        },
    },
    {
        "name": "build_authority_signal",
        "description": "Convert an authority forecast dict (from get_authority_forecast) into a SignalEstimate dict. Only call when post_count > 0 AND the needed metric value (temp_high/temp_low/precip_pct) is present. Pass the full dict returned by get_authority_forecast as authority_forecast.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation"]},
                "threshold": {"type": "number", "description": "Condition threshold; the low edge of the band when operator is 'between'."},
                "threshold_high": {"type": "number", "description": "Upper edge of the band; pass only when operator is 'between' (from parse_weather_market's threshold_high)."},
                "operator": {"type": "string", "enum": ["above", "below", "between"]},
                "authority_forecast": {"type": "object"},
            },
            "required": ["ticker", "metric", "threshold", "operator", "authority_forecast"],
        },
    },
]


class WeatherAgent:
    def __init__(self) -> None:
        self._noaa = NOAAClient()
        self._open_meteo = OpenMeteoClient()
        self._x = XClient()
        # Per-run forecast point: when the series resolves to an NWS settlement
        # station, the ensemble/NOAA forecasts are taken at the station's coords
        # (set in ``run``) instead of the LLM-passed city centroid. Defaults make
        # the handlers safe even if ``run`` did not resolve a station.
        self._forecast_override_coords: tuple[float, float] | None = None
        self._forecast_point: str = "centroid"
        system_prompt = (_PROMPTS_DIR / "weather.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "parse_weather_market": _parse_weather_market,
                "get_noaa_forecast": self._get_noaa_forecast,
                "get_nws_discussion": self._get_nws_discussion,
                "build_weather_signal": self._build_weather_signal,
                "get_ensemble_forecast": self._get_ensemble_forecast,
                "build_ensemble_signal": self._build_ensemble_signal,
                "get_observed_extreme": self._get_observed_extreme,
                "get_authority_forecast": self._get_authority_forecast,
                "build_authority_signal": self._build_authority_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(
        self, ticker: str, title: str, settlement_context: str | None = None
    ) -> list[SignalEstimate]:
        # Resolve the contract's settlement-station coordinates once per run so the
        # ensemble/NOAA forecasts are taken at the station the contract settles on
        # (e.g. LAX) rather than the LLM-passed city centroid (downtown LA), which
        # had been manufacturing spurious edges. Falls back to the centroid when no
        # NWS station resolves (AccuWeather series, no cached terms, fetch failure).
        try:
            self._forecast_override_coords = await resolve_station_coordinates(ticker, self._noaa)
        except Exception:
            self._forecast_override_coords = None
        self._forecast_point = (
            station_label_for_series(ticker) if self._forecast_override_coords else "centroid"
        )

        prompt = f"Analyze this Kalshi weather market:\nticker: {ticker}\ntitle: {title}"
        if settlement_context:
            # The block carries the "measure-the-same-thing" instruction, so the
            # agent forecasts off the contract's settlement source/station (e.g.
            # AccuWeather, not NOAA) or down-weights when it cannot.
            prompt += f"\n\n{settlement_context}"
        raw = await self._agent.run(prompt)
        return self._parse_estimates(raw)

    def _parse_estimates(self, raw: str) -> list[SignalEstimate]:
        return parse_signal_estimates(raw)

    def _stamp_forecast_point(self, estimate_dict: dict) -> dict:
        """Record where the forecast was taken (``station:KXXX`` vs ``centroid``)
        in the signal's metadata, so the deliverable can flag station-resolved
        rows and the centroid fallbacks are auditable."""
        metadata = estimate_dict.get("metadata")
        if isinstance(metadata, dict):
            metadata["forecast_point"] = getattr(self, "_forecast_point", "centroid")
        return estimate_dict

    async def _get_noaa_forecast(self, lat: float, lon: float, date: str) -> dict:
        forecast_lat, forecast_lon = getattr(self, "_forecast_override_coords", None) or (lat, lon)
        target = date_type.fromisoformat(date)
        result = await self._noaa.get_forecast(forecast_lat, forecast_lon, target)
        generated_at = result["generated_at"]
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - generated_at).total_seconds() / 60
        return {
            "temp_high": result["temp_high"],
            "temp_low": result["temp_low"],
            "precip_pct": result["precip_pct"],
            "wind_mph": result["wind_mph"],
            "short_forecast": result["short_forecast"],
            "data_age_minutes": round(age, 1),
        }

    async def _get_nws_discussion(self, lat: float, lon: float) -> dict:
        result = await self._noaa.get_discussion(lat, lon)
        parsed = parse_discussion(result["text"])
        return {
            "confidence": parsed["confidence"],
            "key_points": parsed["key_points"],
        }

    async def _build_weather_signal(
        self,
        ticker: str,
        metric: str,
        threshold: float,
        operator: str,
        forecast: dict,
        discussion: dict | None = None,
        threshold_high: float | None = None,
    ) -> dict:
        estimate = build_weather_signal(
            ticker, metric, threshold, operator, forecast, discussion, threshold_high
        )
        return self._stamp_forecast_point(estimate_to_dict(estimate))

    async def _get_ensemble_forecast(
        self, lat: float, lon: float, date: str, metric: str
    ) -> dict:
        forecast_lat, forecast_lon = getattr(self, "_forecast_override_coords", None) or (lat, lon)
        target = date_type.fromisoformat(date)
        return await self._open_meteo.get_ensemble_members(forecast_lat, forecast_lon, target, metric)

    async def _build_ensemble_signal(
        self,
        ticker: str,
        metric: str,
        threshold: float,
        operator: str,
        ensemble: dict,
        threshold_high: float | None = None,
        observation: dict | None = None,
        lock_fraction: float | None = None,
    ) -> dict:
        # The lock fraction is computed in _get_observed_extreme and rides along in
        # the observation dict; read it back unless the caller passed it explicitly.
        if lock_fraction is None:
            lock_fraction = (observation or {}).get("lock_fraction", 0.0) if isinstance(observation, dict) else 0.0
        estimate = build_ensemble_signal(
            ticker, metric, threshold, operator, ensemble, threshold_high,
            observation, lock_fraction,
        )
        return self._stamp_forecast_point(estimate_to_dict(estimate))

    async def _get_observed_extreme(
        self,
        series_ticker: str,
        target_date: str,
        metric: str,
        station_id: str | None = None,
    ) -> dict:
        """Resolve the settlement station and fetch the realized extreme so far.

        Station resolution is authoritative (the series' settlement source), never
        a guessed airport. Returns ``station_resolved: False`` (override skipped)
        when the override is disabled, the series has no cached NWS-station
        settlement source, or no station id is supplied.
        """
        not_resolved = {"realized_extreme": None, "station_resolved": False}
        if not cfg.get("enable_observation_override"):
            return {**not_resolved, "reason": "observation override disabled by config"}

        if station_id:
            resolved_station = station_id
        else:
            series_prefix = series_ticker.split("-", 1)[0].upper()
            terms_entry = load_contract_terms().get(series_prefix)
            resolved = resolve_settlement_station(
                series_prefix, (terms_entry or {}).get("settlement_sources")
            )
            if not resolved or not resolved.get("station_id"):
                return {**not_resolved, "reason": "no NWS settlement station resolved for this series"}
            resolved_station = resolved["station_id"]

        target = date_type.fromisoformat(target_date)
        observation = await self._noaa.get_observed_extreme(resolved_station, target, metric)
        observation["lock_fraction"] = observation_lock_fraction(metric, observation)
        observation["station_resolved"] = observation.get("realized_extreme") is not None
        return observation

    async def _get_authority_forecast(self, city: str, target_date: str, metric: str) -> dict:
        """Poll the city's named meteorologist authorities via Grok.

        Short-circuits to ``{"post_count": 0, "no_handles": True}`` (no Grok call)
        when the city has no mapped authorities. Otherwise attaches ``handles``,
        ``independent_of_noaa`` and ``data_age_minutes`` to the forecast.
        """
        handles = get_authorities(city)
        if not handles:
            return {"post_count": 0, "no_handles": True}

        forecast = dict(await self._x.forecast_search(handles, city, target_date, metric))
        forecast["handles"] = handles
        forecast["independent_of_noaa"] = is_independent_authority(handles)

        # data_age_minutes from the most-recent-post timestamp (mirrors _get_noaa_forecast).
        issued_at = forecast.get("issued_at")
        age = 0.0
        if issued_at:
            try:
                issued_datetime = datetime.fromisoformat(str(issued_at).replace("Z", "+00:00"))
                if issued_datetime.tzinfo is None:
                    issued_datetime = issued_datetime.replace(tzinfo=timezone.utc)
                age = max(0.0, (datetime.now(tz=timezone.utc) - issued_datetime).total_seconds() / 60)
            except (ValueError, TypeError):
                age = 0.0
        forecast["data_age_minutes"] = round(age, 1)
        return forecast

    async def _build_authority_signal(
        self,
        ticker: str,
        metric: str,
        threshold: float,
        operator: str,
        authority_forecast: dict,
        threshold_high: float | None = None,
    ) -> dict:
        # The independence flag travels in the forecast dict (set by
        # _get_authority_forecast). Default to independent if absent.
        independent_of_noaa = authority_forecast.get("independent_of_noaa", True)
        estimate = build_authority_signal(
            ticker, metric, threshold, operator, authority_forecast,
            independent_of_noaa, threshold_high,
        )
        return estimate_to_dict(estimate)

    async def close(self) -> None:
        await self._noaa.close()
        await self._open_meteo.close()
        await self._x.close()
