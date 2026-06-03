from __future__ import annotations
from datetime import date as date_type, datetime, timezone
from pathlib import Path
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.noaa import NOAAClient
from kalshi_trader.external.weather_parser import parse_title, parse_discussion
from kalshi_trader.external.weather_authorities import get_authorities, is_independent_authority
from kalshi_trader.external.x_client import XClient
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.weather import build_weather_signal, build_authority_signal

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
                "threshold": {"type": "number"},
                "operator": {"type": "string", "enum": ["above", "below"]},
                "forecast": {"type": "object"},
                "discussion": {"type": "object"},
            },
            "required": ["ticker", "metric", "threshold", "operator", "forecast"],
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
                "threshold": {"type": "number"},
                "operator": {"type": "string", "enum": ["above", "below"]},
                "authority_forecast": {"type": "object"},
            },
            "required": ["ticker", "metric", "threshold", "operator", "authority_forecast"],
        },
    },
]


class WeatherAgent:
    def __init__(self) -> None:
        self._noaa = NOAAClient()
        self._x = XClient()
        system_prompt = (_PROMPTS_DIR / "weather.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "parse_weather_market": _parse_weather_market,
                "get_noaa_forecast": self._get_noaa_forecast,
                "get_nws_discussion": self._get_nws_discussion,
                "build_weather_signal": self._build_weather_signal,
                "get_authority_forecast": self._get_authority_forecast,
                "build_authority_signal": self._build_authority_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(
        self, ticker: str, title: str, settlement_context: str | None = None
    ) -> list[SignalEstimate]:
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

    async def _get_noaa_forecast(self, lat: float, lon: float, date: str) -> dict:
        target = date_type.fromisoformat(date)
        result = await self._noaa.get_forecast(lat, lon, target)
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
    ) -> dict:
        estimate = build_weather_signal(ticker, metric, threshold, operator, forecast, discussion)
        return estimate_to_dict(estimate)

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
    ) -> dict:
        # The independence flag travels in the forecast dict (set by
        # _get_authority_forecast). Default to independent if absent.
        independent_of_noaa = authority_forecast.get("independent_of_noaa", True)
        estimate = build_authority_signal(
            ticker, metric, threshold, operator, authority_forecast, independent_of_noaa
        )
        return estimate_to_dict(estimate)

    async def close(self) -> None:
        await self._noaa.close()
        await self._x.close()
