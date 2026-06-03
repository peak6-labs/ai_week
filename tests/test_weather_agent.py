import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from kalshi_trader.agents.weather_agent import _parse_weather_market, WeatherAgent
from kalshi_trader.models import SignalEstimate


@pytest.mark.asyncio
async def test_parse_weather_market_delegates_to_parser():
    result = await _parse_weather_market(
        ticker="WEATHER-NYC-HIGH-JUNE3",
        title="NYC high temp June 3: above 80°F?",
    )
    assert result is not None
    assert result["metric"] == "temp_high"
    assert result["threshold"] == 80.0


@pytest.mark.asyncio
async def test_parse_weather_market_returns_none_for_unparseable():
    result = await _parse_weather_market(ticker="X", title="Some other market")
    assert result is None


def test_parse_estimates_valid_response():
    from kalshi_trader.agents.weather_agent import WeatherAgent
    agent = WeatherAgent.__new__(WeatherAgent)
    raw = '''```json
[
  {
    "source": "noaa_gfs",
    "probability": 0.73,
    "uncertainty": 0.08,
    "weight": 0.85,
    "data_issued_at": "2026-06-02T10:00:00+00:00",
    "metadata": {"ticker": "WEATHER-NYC-RAIN", "data_quality": "fresh"}
  }
]
```'''
    from kalshi_trader.models import SignalEstimate
    results = agent._parse_estimates(raw)
    assert len(results) == 1
    assert isinstance(results[0], SignalEstimate)
    assert results[0].source == "noaa_gfs"
    assert results[0].probability == 0.73


def test_parse_estimates_empty():
    agent = WeatherAgent.__new__(WeatherAgent)
    assert agent._parse_estimates("```json\n[]\n```") == []


def test_parse_estimates_two_element_array():
    agent = WeatherAgent.__new__(WeatherAgent)
    raw = '''```json
[
  {
    "source": "noaa_gfs",
    "probability": 0.73,
    "uncertainty": 0.08,
    "weight": 0.85,
    "data_issued_at": "2026-06-02T10:00:00+00:00",
    "metadata": {"ticker": "WEATHER-DAL-HIGH", "data_quality": "fresh"}
  },
  {
    "source": "x_weather_authority",
    "probability": 0.71,
    "uncertainty": 0.10,
    "weight": 0.70,
    "data_issued_at": "2026-06-02T11:15:00+00:00",
    "metadata": {"ticker": "WEATHER-DAL-HIGH", "independent_of_noaa": true, "post_count": 2}
  }
]
```'''
    results = agent._parse_estimates(raw)
    assert len(results) == 2
    assert all(isinstance(estimate, SignalEstimate) for estimate in results)
    assert [estimate.source for estimate in results] == ["noaa_gfs", "x_weather_authority"]


# ---------------------------------------------------------------------------
# get_authority_forecast handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_authority_forecast_unmapped_city_skips_grok():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._x = MagicMock()
    agent._x.forecast_search = AsyncMock()

    result = await agent._get_authority_forecast("san diego", "2026-06-05", "temp_high")

    assert result == {"post_count": 0, "no_handles": True}
    agent._x.forecast_search.assert_not_called()


@pytest.mark.asyncio
async def test_get_authority_forecast_mapped_city_calls_grok_with_right_handles():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._x = MagicMock()
    agent._x.forecast_search = AsyncMock(return_value={
        "temp_high": 88, "temp_low": 71, "precip_pct": 20, "confidence": "high",
        "post_count": 2, "issued_at": datetime.now(tz=timezone.utc).isoformat(),
        "summary": "", "key_quotes": [],
    })

    result = await agent._get_authority_forecast("dallas", "2026-06-05", "temp_high")

    agent._x.forecast_search.assert_awaited_once_with(
        ["wfaaweather"], "dallas", "2026-06-05", "temp_high"
    )
    assert result["handles"] == ["wfaaweather"]
    assert result["independent_of_noaa"] is True
    assert "data_age_minutes" in result


@pytest.mark.asyncio
async def test_get_authority_forecast_nws_office_city_is_not_independent():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._x = MagicMock()
    agent._x.forecast_search = AsyncMock(return_value={
        "temp_high": 70, "temp_low": 55, "precip_pct": 40, "confidence": "medium",
        "post_count": 1, "issued_at": datetime.now(tz=timezone.utc).isoformat(),
        "summary": "", "key_quotes": [],
    })

    result = await agent._get_authority_forecast("boston", "2026-06-05", "temp_high")

    agent._x.forecast_search.assert_awaited_once_with(
        ["NWSBoston"], "boston", "2026-06-05", "temp_high"
    )
    assert result["handles"] == ["NWSBoston"]
    assert result["independent_of_noaa"] is False


@pytest.mark.asyncio
async def test_build_authority_signal_handler_reads_independence_from_dict():
    agent = WeatherAgent.__new__(WeatherAgent)
    authority_forecast = {
        "temp_high": 88, "temp_low": 71, "precip_pct": 0, "confidence": "high",
        "post_count": 2, "issued_at": datetime.now(tz=timezone.utc).isoformat(),
        "handles": ["NWSBoston"], "independent_of_noaa": False,
    }
    result = await agent._build_authority_signal(
        ticker="KXHIGHTBOS-26JUN05-T85", metric="temp_high", threshold=85.0,
        operator="above", authority_forecast=authority_forecast,
    )
    assert result["source"] == "x_weather_authority"
    assert result["metadata"]["independent_of_noaa"] is False
