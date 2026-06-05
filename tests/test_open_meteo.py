"""Tests for kalshi_trader/external/open_meteo.py (GEFS ensemble client)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from kalshi_trader.external.open_meteo import OpenMeteoClient


def _utc_today():
    return datetime.now(tz=timezone.utc).date()


def _build_daily_payload(field: str, dates: list[str], values_by_date: dict[str, list]) -> dict:
    """Build a minimal Open-Meteo ensemble payload.

    ``values_by_date`` maps each date in ``dates`` to a 31-element column
    (control first, then member01..member30). The series are emitted in that
    order so the client's collection order matches the column order.
    """
    series_names = [field] + [f"{field}_member{member:02d}" for member in range(1, 31)]
    daily: dict = {"time": list(dates)}
    for series_index, series_name in enumerate(series_names):
        daily[series_name] = [values_by_date[d][series_index] for d in dates]
    return {"daily": daily, "daily_units": {field: "°F"}}


@pytest.mark.asyncio
async def test_get_ensemble_members_returns_31_members_for_target_date():
    target = _utc_today() + timedelta(days=1)
    dates = [(_utc_today() + timedelta(days=offset)).isoformat() for offset in range(3)]
    target_iso = target.isoformat()
    target_values = [50.0 + index for index in range(31)]
    values_by_date = {dates[0]: [0.0] * 31, target_iso: target_values, dates[2]: [0.0] * 31}
    payload = _build_daily_payload("temperature_2m_max", dates, values_by_date)

    client = OpenMeteoClient()
    client._get = AsyncMock(return_value=payload)
    result = await client.get_ensemble_members(41.8781, -87.6298, target, "temp_high")

    assert result["member_count"] == 31
    assert result["members"] == target_values
    assert result["field"] == "temperature_2m_max"
    assert result["units"] == "°F"
    assert result["model"] == "gfs_seamless"
    assert result["data_issued_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_get_ensemble_members_precipitation_maps_to_precip_field():
    target = _utc_today() + timedelta(days=1)
    dates = [(_utc_today() + timedelta(days=offset)).isoformat() for offset in range(2)]
    target_iso = target.isoformat()
    target_values = [round(0.01 * index, 2) for index in range(31)]
    values_by_date = {dates[0]: [0.0] * 31, target_iso: target_values}
    payload = _build_daily_payload("precipitation_sum", dates, values_by_date)

    client = OpenMeteoClient()
    client._get = AsyncMock(return_value=payload)
    result = await client.get_ensemble_members(41.8781, -87.6298, target, "precipitation")

    assert result["field"] == "precipitation_sum"
    assert result["member_count"] == 31
    assert result["members"] == target_values


@pytest.mark.asyncio
async def test_get_ensemble_members_target_date_not_in_range_returns_empty():
    # The payload covers dates that do NOT include the (in-horizon) target date.
    target = _utc_today() + timedelta(days=1)
    dates = [(_utc_today() + timedelta(days=offset)).isoformat() for offset in (0, 2, 3)]
    values_by_date = {d: [0.0] * 31 for d in dates}
    payload = _build_daily_payload("temperature_2m_max", dates, values_by_date)

    client = OpenMeteoClient()
    client._get = AsyncMock(return_value=payload)
    result = await client.get_ensemble_members(41.8781, -87.6298, target, "temp_high")

    assert result["members"] == []
    assert result["member_count"] == 0


@pytest.mark.asyncio
async def test_get_ensemble_members_past_date_skips_http():
    target = _utc_today() - timedelta(days=1)
    client = OpenMeteoClient()
    client._get = AsyncMock()
    result = await client.get_ensemble_members(41.8781, -87.6298, target, "temp_high")

    assert result["members"] == []
    assert result["member_count"] == 0
    client._get.assert_not_called()


@pytest.mark.asyncio
async def test_get_ensemble_members_beyond_horizon_skips_http():
    target = _utc_today() + timedelta(days=20)
    client = OpenMeteoClient()
    client._get = AsyncMock()
    result = await client.get_ensemble_members(41.8781, -87.6298, target, "temp_high")

    assert result["members"] == []
    client._get.assert_not_called()


@pytest.mark.asyncio
async def test_get_ensemble_members_skips_none_members():
    target = _utc_today() + timedelta(days=1)
    dates = [(_utc_today() + timedelta(days=offset)).isoformat() for offset in range(2)]
    target_iso = target.isoformat()
    target_values: list = [50.0 + index for index in range(31)]
    for missing_index in (5, 10, 15):
        target_values[missing_index] = None
    values_by_date = {dates[0]: [0.0] * 31, target_iso: target_values}
    payload = _build_daily_payload("temperature_2m_max", dates, values_by_date)

    client = OpenMeteoClient()
    client._get = AsyncMock(return_value=payload)
    result = await client.get_ensemble_members(41.8781, -87.6298, target, "temp_high")

    assert result["member_count"] == 28
    assert None not in result["members"]
    assert result["members"] == [value for value in target_values if value is not None]


@pytest.mark.asyncio
async def test_get_ensemble_members_unknown_metric_skips_http():
    target = _utc_today() + timedelta(days=1)
    client = OpenMeteoClient()
    client._get = AsyncMock()
    result = await client.get_ensemble_members(41.8781, -87.6298, target, "wind")

    assert result["members"] == []
    assert result["member_count"] == 0
    client._get.assert_not_called()


@pytest.mark.asyncio
async def test_get_ensemble_members_same_day_utc_finds_members_when_api_starts_with_prior_local_day():
    # Regression: when days_ahead=0 (target == UTC today), timezone=auto causes
    # the API to return dates starting at the *local* date (e.g. still June 4 in
    # US timezones when UTC has already ticked to June 5). The fix requests
    # forecast_days = days_ahead + 2 so the UTC target date always falls inside
    # the returned window.
    target = _utc_today()  # days_ahead = 0
    prior_day = (target - timedelta(days=1)).isoformat()
    target_iso = target.isoformat()
    dates = [prior_day, target_iso]
    target_values = [70.0 + index for index in range(31)]
    values_by_date = {prior_day: [0.0] * 31, target_iso: target_values}
    payload = _build_daily_payload("temperature_2m_max", dates, values_by_date)

    client = OpenMeteoClient()
    client._get = AsyncMock(return_value=payload)
    result = await client.get_ensemble_members(41.8781, -87.6298, target, "temp_high")

    assert result["member_count"] == 31
    assert result["members"] == target_values


@pytest.mark.asyncio
async def test_close_without_session_is_noop():
    client = OpenMeteoClient()
    await client.close()  # must not raise when no session was ever opened
