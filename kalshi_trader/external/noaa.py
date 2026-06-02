from __future__ import annotations
import re
from datetime import date, datetime
import aiohttp

NWS_BASE = "https://api.weather.gov"
_HEADERS = {"User-Agent": "kalshi-trader/1.0 scorley@peak6.com", "Accept": "application/geo+json"}


def _parse_wind_mph(wind_str: str) -> float:
    range_match = re.search(r"(\d+)\s+to\s+(\d+)", wind_str)
    if range_match:
        return (float(range_match.group(1)) + float(range_match.group(2))) / 2
    single = re.search(r"(\d+)", wind_str)
    return float(single.group(1)) if single else 0.0


class NOAAClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._grid_cache: dict[tuple[float, float], dict] = {}

    async def _get(self, url: str) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=_HEADERS)
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _grid(self, lat: float, lon: float) -> dict:
        key = (round(lat, 4), round(lon, 4))
        if key not in self._grid_cache:
            data = await self._get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
            props = data["properties"]
            self._grid_cache[key] = {
                "forecast_url": props["forecast"],
                "hourly_url": props["forecastHourly"],
                "wfo": props["cwa"],
            }
        return self._grid_cache[key]

    async def get_forecast(self, lat: float, lon: float, target_date: date) -> dict:
        grid = await self._grid(lat, lon)
        data = await self._get(grid["forecast_url"])
        props = data["properties"]
        generated_at = datetime.fromisoformat(props["generatedAt"].replace("Z", "+00:00")).replace(tzinfo=None)

        temp_high: float | None = None
        temp_low: float | None = None
        precip_pct = 0
        wind_mph = 0.0
        short_forecast = ""

        for period in props["periods"]:
            start = datetime.fromisoformat(period["startTime"])
            if start.date() != target_date:
                continue
            precip = (period.get("probabilityOfPrecipitation") or {}).get("value") or 0
            wind = _parse_wind_mph(period.get("windSpeed", ""))
            if period["isDaytime"]:
                temp_high = float(period["temperature"])
                precip_pct = int(precip)
                wind_mph = wind
                short_forecast = period.get("shortForecast", "")
            else:
                temp_low = float(period["temperature"])

        return {
            "temp_high": temp_high,
            "temp_low": temp_low,
            "precip_pct": precip_pct,
            "wind_mph": wind_mph,
            "short_forecast": short_forecast,
            "generated_at": generated_at,
        }

    async def get_discussion(self, lat: float, lon: float) -> dict:
        grid = await self._grid(lat, lon)
        products = await self._get(f"{NWS_BASE}/products?type=AFD&location={grid['wfo']}")
        graph = products.get("@graph", [])
        if not graph:
            return {"text": "", "issuance_time": datetime.utcnow()}
        product = await self._get(graph[0]["@id"])
        issuance_time = datetime.fromisoformat(
            product["issuanceTime"].replace("Z", "+00:00")
        ).replace(tzinfo=None)
        return {"text": product.get("productText", ""), "issuance_time": issuance_time}

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
