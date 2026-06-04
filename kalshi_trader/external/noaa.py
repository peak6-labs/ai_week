from __future__ import annotations
import re
import ssl
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import aiohttp

NWS_BASE = "https://api.weather.gov"
_HEADERS = {"User-Agent": "kalshi-trader/1.0 scorley@peak6.com", "Accept": "application/geo+json"}


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context that trusts the OS trust store.

    Behind the corporate proxy (Zscaler) the NWS cert chain ends in a
    self-signed root that only lives in the system trust store, not certifi's
    bundle — so a default aiohttp context fails verification. truststore reads
    the OS store and fixes this (same reason db.py injects truststore for httpx).
    Falls back to the default context if truststore is unavailable.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


def _parse_wind_mph(wind_str: str) -> float:
    range_match = re.search(r"(\d+)\s+to\s+(\d+)", wind_str)
    if range_match:
        return (float(range_match.group(1)) + float(range_match.group(2))) / 2
    single = re.search(r"(\d+)", wind_str)
    return float(single.group(1)) if single else 0.0


def _observation_station_candidates(station_id: str) -> list[str]:
    """Candidate api.weather.gov station ids for a settlement station code.

    NWS climatological reports name a 3-letter office/station code (e.g. ``LAX``),
    but api.weather.gov observation stations use the 4-letter ICAO id (``KLAX`` in
    the contiguous US). Try the ICAO form first for a bare 3-letter code, then the
    code as given. (Alaska/Hawaii/territory prefixes are out of scope for v1.)
    """
    normalized = (station_id or "").strip().upper()
    if not normalized:
        return []
    candidates = [f"K{normalized}", normalized] if (len(normalized) == 3 and normalized.isalpha()) else [normalized]
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _precip_to_inches(value: float, unit_code: str) -> float:
    """Convert an api.weather.gov precipitation value to inches (best-effort).

    The observation precip fields are reported in millimeters (``wmoUnit:mm``) by
    default; meters is handled defensively.
    """
    unit = (unit_code or "").lower()
    if unit.endswith(":m") or unit.endswith("/m"):
        return value * 39.3701
    return value / 25.4  # default: millimeters


class NOAAClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._grid_cache: dict[tuple[float, float], dict] = {}

    async def _get(self, url: str) -> dict:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
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

    async def get_station_coordinates(self, station_id: str) -> tuple[float, float] | None:
        """Return ``(lat, lon)`` for an NWS settlement station, or ``None``.

        Reuses the same candidate ids and ``/stations/{id}`` endpoint that
        ``get_observed_extreme`` already hits, but reads ``geometry.coordinates``
        (GeoJSON ``[lon, lat]``) instead of ``timeZone``. Used to forecast the
        ensemble at the station the contract actually settles on rather than the
        city centroid. Returns ``None`` when no candidate resolves or the station
        object carries no usable point geometry.
        """
        for candidate in _observation_station_candidates(station_id):
            try:
                metadata = await self._get(f"{NWS_BASE}/stations/{candidate}")
            except Exception:  # 404 for the wrong candidate, or transient failure
                continue
            coordinates = ((metadata.get("geometry") or {}).get("coordinates")) or []
            if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
                longitude, latitude = coordinates[0], coordinates[1]
                try:
                    return float(latitude), float(longitude)
                except (TypeError, ValueError):
                    continue
        return None

    async def get_observed_extreme(
        self, station_id: str, target_date_local: date, metric: str
    ) -> dict:
        """Realized daily extreme so far for the contract's settlement station.

        For a same-day market the day's min/max is often already locked in, so the
        live observation can override a stale model forecast (see the
        live-observation override plan). Reads ``api.weather.gov`` observations for
        the station's local calendar day:

          temp_high → max observed temperature (a LOWER bound on the final max)
          temp_low  → min observed temperature (an UPPER bound on the final min)
          precipitation → summed hourly precip (a LOWER bound on the daily total;
            best-effort — METAR precip accounting is noisy)

        Returns a dict; ``realized_extreme is None`` when the station/timezone
        can't be resolved, no observation falls in the window, or the fetch fails —
        the caller then keeps the pure ensemble. Never fabricates a reading.
        """
        empty_result = {
            "station_id": station_id,
            "timezone": None,
            "metric": metric,
            "realized_extreme": None,
            "at_timestamp": None,
            "latest_timestamp": None,
            "obs_count": 0,
        }

        timezone_name: str | None = None
        resolved_station: str | None = None
        for candidate in _observation_station_candidates(station_id):
            try:
                metadata = await self._get(f"{NWS_BASE}/stations/{candidate}")
            except Exception:  # 404 for the wrong candidate, or transient failure
                continue
            timezone_name = (metadata.get("properties") or {}).get("timeZone")
            resolved_station = candidate
            break
        if resolved_station is None or not timezone_name:
            return empty_result

        try:
            station_timezone = ZoneInfo(timezone_name)
        except Exception:
            return {**empty_result, "station_id": resolved_station, "timezone": timezone_name}

        local_day_start = datetime(
            target_date_local.year, target_date_local.month, target_date_local.day,
            tzinfo=station_timezone,
        )
        local_day_end = local_day_start + timedelta(days=1)
        window_start_utc = local_day_start.astimezone(timezone.utc)
        window_end_utc = local_day_end.astimezone(timezone.utc)

        # Z-suffixed, not isoformat() — a literal "+00:00" offset would be decoded
        # as a space in the query string and the window filter would match nothing.
        start_param = window_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_param = window_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        observations_url = (
            f"{NWS_BASE}/stations/{resolved_station}/observations"
            f"?start={start_param}&end={end_param}&limit=500"
        )
        try:
            observations = await self._get(observations_url)
        except Exception:
            return {**empty_result, "station_id": resolved_station, "timezone": timezone_name}

        realized_extreme: float | None = None
        extreme_observed_at: datetime | None = None
        latest_observed_at: datetime | None = None
        observation_count = 0
        precip_total_inches = 0.0
        saw_precip = False

        for feature in observations.get("features", []):
            properties = feature.get("properties") or {}
            observed_at_raw = properties.get("timestamp")
            if not observed_at_raw:
                continue
            try:
                observed_at = datetime.fromisoformat(observed_at_raw.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if not (window_start_utc <= observed_at < window_end_utc):
                continue

            if metric in ("temp_high", "temp_low"):
                temperature_celsius = (properties.get("temperature") or {}).get("value")
                if temperature_celsius is None:
                    continue
                temperature_fahrenheit = temperature_celsius * 9.0 / 5.0 + 32.0
                observation_count += 1
                if latest_observed_at is None or observed_at > latest_observed_at:
                    latest_observed_at = observed_at
                is_new_extreme = (
                    realized_extreme is None
                    or (metric == "temp_high" and temperature_fahrenheit > realized_extreme)
                    or (metric == "temp_low" and temperature_fahrenheit < realized_extreme)
                )
                if is_new_extreme:
                    realized_extreme = round(temperature_fahrenheit, 1)
                    extreme_observed_at = observed_at
            elif metric == "precipitation":
                precip_field = properties.get("precipitationLastHour") or {}
                precip_value = precip_field.get("value")
                if precip_value is None:
                    continue
                observation_count += 1
                saw_precip = True
                precip_total_inches += _precip_to_inches(precip_value, precip_field.get("unitCode", ""))
                if latest_observed_at is None or observed_at > latest_observed_at:
                    latest_observed_at = observed_at
                extreme_observed_at = observed_at

        if metric == "precipitation":
            realized_extreme = round(precip_total_inches, 3) if saw_precip else None

        return {
            "station_id": resolved_station,
            "timezone": timezone_name,
            "metric": metric,
            "realized_extreme": realized_extreme,
            "at_timestamp": extreme_observed_at.isoformat() if extreme_observed_at else None,
            "latest_timestamp": latest_observed_at.isoformat() if latest_observed_at else None,
            "obs_count": observation_count,
        }

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
