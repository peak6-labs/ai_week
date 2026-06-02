from __future__ import annotations
from datetime import date as date_type, datetime, timezone
from pathlib import Path
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.noaa import NOAAClient
from kalshi_trader.external.weather_parser import parse_title, parse_discussion
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict
from kalshi_trader.signals.weather import build_weather_signal

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
]


class WeatherAgent:
    def __init__(self) -> None:
        self._noaa = NOAAClient()
        system_prompt = (_PROMPTS_DIR / "weather.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "parse_weather_market": _parse_weather_market,
                "get_noaa_forecast": self._get_noaa_forecast,
                "get_nws_discussion": self._get_nws_discussion,
                "build_weather_signal": self._build_weather_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(self, ticker: str, title: str) -> list[SignalEstimate]:
        prompt = f"Analyze this Kalshi weather market:\nticker: {ticker}\ntitle: {title}"
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

    async def close(self) -> None:
        await self._noaa.close()
