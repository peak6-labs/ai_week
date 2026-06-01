from __future__ import annotations
import json
import math
import re
from datetime import datetime, timedelta
from typing import Any
import scipy.stats
from kalshi_trader.models import Market, TradeIdea, Side, OrderAction
from kalshi_trader.external.noaa import NOAAClient
from kalshi_trader.external.weather_parser import parse_title, parse_discussion
from kalshi_trader.agents.base import BaseAgent


# ---------------------------------------------------------------------------
# Module-level tool handlers (pure functions, no instance state)
# ---------------------------------------------------------------------------

async def _parse_weather_market(ticker: str, title: str) -> dict | None:
    return parse_title(ticker, title)


async def _estimate_probability(
    metric: str,
    threshold: float,
    operator: str,
    forecast: dict,
) -> dict:
    data_age = forecast.get("data_age_minutes", 0)
    issued_at = (datetime.utcnow() - timedelta(minutes=data_age)).isoformat()

    if metric in ("temp_high", "temp_low"):
        high = forecast.get("temp_high") or 85.0
        low = forecast.get("temp_low") or 65.0
        mean = (high + low) / 2.0
        std = max((high - low) / 4.0, 1.0)
        dist = scipy.stats.norm(mean, std)
        prob = float(dist.sf(threshold) if operator == "above" else dist.cdf(threshold))
        uncertainty = 0.08
    elif metric == "precipitation":
        prob = (forecast.get("precip_pct") or 0) / 100.0
        uncertainty = 0.05
    else:
        return {"error": f"Unsupported metric: {metric}"}

    return {
        "source": "noaa_gfs",
        "probability": round(min(max(prob, 0.01), 0.99), 4),
        "uncertainty": uncertainty,
        "weight": 0.85,
        "data_issued_at": issued_at,
        "metadata": {"metric": metric, "threshold": threshold, "operator": operator},
    }


async def _combine_signals(estimates: list[dict]) -> dict:
    if not estimates:
        return {"error": "No estimates provided"}

    total_w = 0.0
    w_prob = 0.0
    w_unc = 0.0
    max_staleness = 0.0

    for e in estimates:
        issued = datetime.fromisoformat(e["data_issued_at"])
        staleness = (datetime.utcnow() - issued).total_seconds() / 60
        eff_w = e["weight"] * math.exp(-staleness / 360.0)
        total_w += eff_w
        w_prob += eff_w * e["probability"]
        w_unc += eff_w * e["uncertainty"]
        max_staleness = max(max_staleness, staleness)

    if total_w == 0:
        return {"error": "All estimates have zero effective weight"}

    combined_prob = w_prob / total_w
    combined_unc = w_unc / total_w

    if len(estimates) > 1:
        probs = [e["probability"] for e in estimates]
        spread = max(probs) - min(probs)
        if spread > 0.10:
            combined_unc += spread * 0.5

    return {
        "combined_probability": round(combined_prob, 4),
        "uncertainty": round(combined_unc, 4),
        "staleness_minutes": round(max_staleness, 1),
        "n_sources": len(estimates),
    }


async def _calculate_edge(combined_probability: float, market_price_cents: float) -> dict:
    edge = combined_probability * 100 - market_price_cents
    c = market_price_cents / 100.0
    fee = 0.07 * c * (1.0 - c) * 100
    adj = edge - fee
    return {
        "edge_cents": round(edge, 2),
        "fee_adjusted_edge": round(adj, 2),
        "worth_trading": adj > 5.0,
    }


# ---------------------------------------------------------------------------
# Tool JSON schemas
# ---------------------------------------------------------------------------

_SCHEMAS: list[dict] = [
    {
        "name": "list_weather_markets",
        "description": "List all open Kalshi weather markets with ticker, title, yes_price, volume_24h, and hours_to_close.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "parse_weather_market",
        "description": "Parse a Kalshi weather market title into a structured question (city, lat, lon, metric, threshold, operator, target_date). Returns null if unparseable — skip that market.",
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
        "name": "estimate_probability",
        "description": "Estimate the probability a weather condition meets the threshold using NOAA forecast data. Pass the full forecast dict from get_noaa_forecast.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["temp_high", "temp_low", "precipitation", "wind"]},
                "threshold": {"type": "number"},
                "operator": {"type": "string", "enum": ["above", "below"]},
                "forecast": {"type": "object"},
            },
            "required": ["metric", "threshold", "operator", "forecast"],
        },
    },
    {
        "name": "get_nws_discussion",
        "description": "Fetch and parse the NWS Area Forecast Discussion for a location. Returns confidence level ('high'/'medium'/'low') and key uncertainty sentences. Use for qualitative reasoning only — do NOT pass to combine_signals.",
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
        "name": "combine_signals",
        "description": "Combine a list of SignalEstimate dicts into one probability using staleness-discounted weighted averaging. Each estimate must have: source, probability, uncertainty, weight, data_issued_at (ISO string).",
        "input_schema": {
            "type": "object",
            "properties": {
                "estimates": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["estimates"],
        },
    },
    {
        "name": "calculate_edge",
        "description": "Calculate fee-adjusted edge between estimated probability and the current Kalshi YES ask price. Returns edge_cents, fee_adjusted_edge, and worth_trading (true if fee_adjusted_edge > 5 cents).",
        "input_schema": {
            "type": "object",
            "properties": {
                "combined_probability": {"type": "number"},
                "market_price_cents": {"type": "number"},
            },
            "required": ["combined_probability", "market_price_cents"],
        },
    },
]

_SYSTEM_PROMPT = """\
You are a weather market specialist for a Kalshi prediction market trading system.

Your job: identify Kalshi weather markets where NOAA forecast data implies a meaningfully different probability than the current market price.

## Workflow
1. If markets are not provided in the user message, call list_weather_markets.
2. For each market with volume_24h > 1000 and hours_to_close > 4:
   a. Call parse_weather_market. If it returns null, skip.
   b. Call get_noaa_forecast with the parsed lat, lon, and target_date.
   c. Call estimate_probability using the metric, threshold, operator, and the full forecast dict.
   d. Optionally call get_nws_discussion if precip_pct is between 30-70% (high uncertainty zone) — use for qualitative context in reasoning only.
   e. Call combine_signals with your estimate(s) from step c.
   f. Call calculate_edge with the combined_probability and the market's yes_price.
3. Only include markets where worth_trading is true.

## Output
End your final response with exactly one fenced JSON block:
```json
[
  {
    "ticker": "WEATHER-NYC-RAIN-JUNE3",
    "side": "yes",
    "confidence": 0.73,
    "market_price": 18.0,
    "reasoning": "NOAA shows 73% precip vs 18 cent market. NWS discussion notes high confidence.",
    "signal_sources": ["noaa_gfs"]
  }
]
```
If no markets are worth trading, output: ```json\n[]\n```
"""


class WeatherAgent:
    def __init__(self, client: Any, scanner: Any) -> None:
        self._kalshi = client
        self._scanner = scanner
        self._noaa = NOAAClient()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "list_weather_markets": self._list_weather_markets,
                "parse_weather_market": _parse_weather_market,
                "get_noaa_forecast": self._get_noaa_forecast,
                "estimate_probability": _estimate_probability,
                "get_nws_discussion": self._get_nws_discussion,
                "combine_signals": _combine_signals,
                "calculate_edge": _calculate_edge,
            },
            system_prompt=_SYSTEM_PROMPT,
        )

    async def run(self, markets: list[Market] | None = None) -> list[TradeIdea]:
        if markets is not None:
            now = datetime.utcnow()
            market_list = [
                {
                    "ticker": m.ticker,
                    "title": m.title,
                    "yes_price": m.yes_ask,
                    "volume_24h": m.volume_24h,
                    "hours_to_close": round(max(0.0, (m.close_time - now).total_seconds() / 3600), 1),
                }
                for m in markets
            ]
            prompt = f"Analyze these weather markets:\n{json.dumps(market_list, indent=2)}"
        else:
            prompt = "Find weather markets with edge."

        raw = await self._agent.run(prompt)
        return self._parse_ideas(raw)

    async def _list_weather_markets(self) -> list[dict]:
        all_markets = await self._scanner.get_open_markets()
        now = datetime.utcnow()
        keywords = ["weather", "temperature", "rain", "precip", "temp", "wind"]
        filtered = [
            m for m in all_markets
            if any(kw in (m.category + " " + m.title).lower() for kw in keywords)
        ]
        return [
            {
                "ticker": m.ticker,
                "title": m.title,
                "yes_price": m.yes_ask,
                "volume_24h": m.volume_24h,
                "hours_to_close": round(max(0.0, (m.close_time - now).total_seconds() / 3600), 1),
            }
            for m in filtered
        ]

    async def _get_noaa_forecast(self, lat: float, lon: float, date: str) -> dict:
        from datetime import date as date_type
        target = date_type.fromisoformat(date)
        result = await self._noaa.get_forecast(lat, lon, target)
        age = (datetime.utcnow() - result["generated_at"]).total_seconds() / 60
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
            "issued_at": result["issuance_time"].isoformat(),
        }

    def _parse_ideas(self, raw: str) -> list[TradeIdea]:
        match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        return [
            TradeIdea(
                agent_id="weather",
                ticker=item["ticker"],
                side=Side(item.get("side", "yes")),
                action=OrderAction.BUY,
                confidence=float(item["confidence"]),
                market_price=float(item["market_price"]),
                reasoning=item.get("reasoning", ""),
                signal_sources=item.get("signal_sources", []),
            )
            for item in data
        ]

    async def close(self) -> None:
        await self._noaa.close()
