"""Open-Meteo GEFS ensemble client.

Fetches the 31-member GEFS daily ensemble (control + member01..member30) for a
lat/lon and returns the per-member values for a target date. The weather signal
turns that member spread into a probability (fraction of members past a
threshold) — the ensemble empirical CDF that replaces the old parametric
normal-CDF proxy.

The Ensemble API (``https://ensemble-api.open-meteo.com/v1/ensemble``) needs no
API key and, with ``timezone=auto``, returns daily max/min already aggregated in
the location's local day — which is the day a Kalshi temperature contract settles
on.
"""
from __future__ import annotations

import ssl
from datetime import date, datetime, timezone

import aiohttp

ENSEMBLE_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
_HEADERS = {"User-Agent": "kalshi-trader/1.0 scorley@peak6.com"}

# Kalshi metric → the Open-Meteo daily field whose members we count.
_METRIC_TO_DAILY_FIELD: dict[str, str] = {
    "temp_high": "temperature_2m_max",
    "temp_low": "temperature_2m_min",
    "precipitation": "precipitation_sum",
}

# GEFS via gfs_seamless reaches ~16 days; never request beyond this.
_MAX_FORECAST_DAYS = 16


def _build_ssl_context() -> ssl.SSLContext:
    """Trust the OS store so the cert chain validates behind the Zscaler proxy.

    Same rationale as ``kalshi_trader.external.noaa._build_ssl_context``: behind
    the corporate proxy the cert chain ends in a root that only lives in the OS
    trust store, not certifi's bundle. Falls back to the default context if
    truststore is unavailable.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


class OpenMeteoClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get(self, params: dict) -> dict:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
        async with self._session.get(
            ENSEMBLE_BASE, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as api_response:
            api_response.raise_for_status()
            return await api_response.json()

    async def get_ensemble_members(
        self, lat: float, lon: float, target_date: date, metric: str
    ) -> dict:
        """Return the per-member GEFS daily values for ``target_date``.

        Returns a dict::

            {"members": list[float], "member_count": int, "field": str,
             "units": str, "model": "gfs_seamless", "data_issued_at": datetime}

        ``members`` is empty when the metric is unsupported, the target date is
        outside the forecast horizon, or the API returns no usable series for it —
        the caller then falls back to the parametric NOAA path.
        """
        daily_field = _METRIC_TO_DAILY_FIELD.get(metric)
        if daily_field is None:
            return {"members": [], "member_count": 0, "field": "", "units": ""}

        days_ahead = (target_date - datetime.now(tz=timezone.utc).date()).days
        if days_ahead < 0 or days_ahead >= _MAX_FORECAST_DAYS:
            return {"members": [], "member_count": 0, "field": daily_field, "units": ""}
        # Add +2 (not +1) so that when timezone=auto shifts the API response to
        # local time, the target date still falls within the requested range even
        # when local time lags UTC by up to one calendar day (e.g. UTC midnight
        # to UTC+14 at most; practically up to ~-12 hours means local date can
        # be one day behind UTC).
        forecast_days = max(1, min(days_ahead + 2, _MAX_FORECAST_DAYS))

        params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "daily": daily_field,
            "models": "gfs_seamless",
            "timezone": "auto",
            "forecast_days": str(forecast_days),
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
        }
        data = await self._get(params)
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        target_iso = target_date.isoformat()
        if target_iso not in dates:
            return {"members": [], "member_count": 0, "field": daily_field, "units": ""}
        row_index = dates.index(target_iso)

        # Collect every member series for this field: the no-suffix control plus
        # ``<field>_memberNN``. Skip None values (a member can be missing at long
        # lead time).
        members: list[float] = []
        for series_name, series_values in daily.items():
            if series_name == daily_field or series_name.startswith(f"{daily_field}_member"):
                if row_index < len(series_values) and series_values[row_index] is not None:
                    members.append(float(series_values[row_index]))

        return {
            "members": members,
            "member_count": len(members),
            "field": daily_field,
            "units": (data.get("daily_units", {}) or {}).get(daily_field, ""),
            "model": "gfs_seamless",
            "data_issued_at": datetime.now(tz=timezone.utc),
        }

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
