import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date, datetime, timezone

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


# ---------------------------------------------------------------------------
# ensemble handlers (GEFS ensemble — primary quantitative signal)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_ensemble_forecast_delegates_to_open_meteo():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._open_meteo = MagicMock()
    agent._open_meteo.get_ensemble_members = AsyncMock(return_value={
        "members": [80.0] * 31, "member_count": 31, "field": "temperature_2m_max",
        "units": "°F", "model": "gfs_seamless",
    })

    result = await agent._get_ensemble_forecast(41.8781, -87.6298, "2026-06-05", "temp_high")

    # The handler parses the YYYY-MM-DD string into a date before delegating.
    agent._open_meteo.get_ensemble_members.assert_awaited_once_with(
        41.8781, -87.6298, date(2026, 6, 5), "temp_high"
    )
    assert result["member_count"] == 31


@pytest.mark.asyncio
async def test_build_ensemble_signal_handler_returns_gfs_ensemble():
    agent = WeatherAgent.__new__(WeatherAgent)
    ensemble = {"members": [70.0 + index for index in range(31)], "member_count": 31}
    result = await agent._build_ensemble_signal(
        ticker="KXHIGHTCHI-26JUN05-T85", metric="temp_high", threshold=85.0,
        operator="above", ensemble=ensemble,
    )
    assert result["source"] == "gfs_ensemble"
    assert result["metadata"]["member_count"] == 31
    # estimate_to_dict must emit data_issued_at as an ISO string.
    assert isinstance(result["data_issued_at"], str)


@pytest.mark.asyncio
async def test_build_ensemble_signal_handler_tolerates_roundtripped_string_timestamp():
    # When the ensemble dict round-trips through the agent, data_issued_at arrives
    # as a string (BaseAgent json-encodes tool results with default=str). The
    # builder must ignore it and still produce an ISO-serializable estimate.
    agent = WeatherAgent.__new__(WeatherAgent)
    ensemble = {
        "members": [70.0 + index for index in range(31)], "member_count": 31,
        "data_issued_at": "2026-06-05 12:00:00+00:00",  # str(datetime), not a datetime
    }
    result = await agent._build_ensemble_signal(
        ticker="T", metric="temp_high", threshold=85.0, operator="above", ensemble=ensemble,
    )
    assert result["source"] == "gfs_ensemble"
    assert isinstance(result["data_issued_at"], str)


@pytest.mark.asyncio
async def test_build_ensemble_signal_handler_threads_threshold_high():
    # Band market: handler must forward threshold_high so the builder counts
    # members inside [85, 86] (only 85 and 86 of the 70..100 ladder).
    agent = WeatherAgent.__new__(WeatherAgent)
    ensemble = {"members": [70.0 + index for index in range(31)], "member_count": 31}
    result = await agent._build_ensemble_signal(
        ticker="KXHIGHTMIN-26JUN03-B85.5", metric="temp_high", threshold=85.0,
        operator="between", ensemble=ensemble, threshold_high=86.0,
    )
    assert result["source"] == "gfs_ensemble"
    assert result["metadata"]["members_satisfying"] == 2


def test_weather_agent_schemas_support_between_and_threshold_high():
    from kalshi_trader.agents.weather_agent import _SCHEMAS
    build_tools = {
        schema["name"]: schema for schema in _SCHEMAS
        if schema["name"] in (
            "build_weather_signal", "build_ensemble_signal", "build_authority_signal"
        )
    }
    assert len(build_tools) == 3
    for schema in build_tools.values():
        properties = schema["input_schema"]["properties"]
        assert "between" in properties["operator"]["enum"]
        assert "threshold_high" in properties


# ---------------------------------------------------------------------------
# Live-observation override wiring
# ---------------------------------------------------------------------------

def test_weather_agent_schemas_include_observation_override():
    from kalshi_trader.agents.weather_agent import _SCHEMAS
    by_name = {schema["name"]: schema for schema in _SCHEMAS}
    assert "get_observed_extreme" in by_name
    assert "observation" in by_name["build_ensemble_signal"]["input_schema"]["properties"]


@pytest.mark.asyncio
async def test_get_observed_extreme_with_explicit_station_delegates_and_adds_lock():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._noaa = MagicMock()
    agent._noaa.get_observed_extreme = AsyncMock(return_value={
        "station_id": "KATL", "timezone": "America/New_York",
        "realized_extreme": 57.2, "at_timestamp": "2026-06-03T09:50:00+00:00",
        "latest_timestamp": "2026-06-03T19:45:00+00:00", "obs_count": 200,
    })

    result = await agent._get_observed_extreme(
        "KXLOWTATL-26JUN03-B57.5", "2026-06-03", "temp_low", station_id="KATL"
    )

    agent._noaa.get_observed_extreme.assert_awaited_once_with("KATL", date(2026, 6, 3), "temp_low")
    assert result["realized_extreme"] == 57.2
    assert result["station_resolved"] is True
    # 19:45Z is 15:45 EDT — well past the temp_low lock hour → lock_fraction 1.0.
    assert result["lock_fraction"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_get_observed_extreme_resolves_station_from_cached_terms():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._noaa = MagicMock()
    agent._noaa.get_observed_extreme = AsyncMock(return_value={
        "station_id": "KLAX", "timezone": "America/Los_Angeles",
        "realized_extreme": 78.0, "at_timestamp": None,
        "latest_timestamp": "2026-06-03T22:00:00+00:00", "obs_count": 50,
    })
    fake_terms = {
        "KXHIGHLAX": {"settlement_sources": [
            {"name": "NWS", "url": "https://forecast.weather.gov/product.php?product=CLI&issuedby=LAX"}
        ]}
    }
    with patch("kalshi_trader.agents.weather_agent.load_contract_terms", return_value=fake_terms):
        result = await agent._get_observed_extreme("KXHIGHLAX-26JUN03-T80", "2026-06-03", "temp_high")

    # Resolved from settlement source (issuedby=LAX), not a guessed airport.
    agent._noaa.get_observed_extreme.assert_awaited_once_with("LAX", date(2026, 6, 3), "temp_high")
    assert result["station_resolved"] is True


@pytest.mark.asyncio
async def test_get_observed_extreme_skips_when_no_station_resolved():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._noaa = MagicMock()
    agent._noaa.get_observed_extreme = AsyncMock()
    with patch("kalshi_trader.agents.weather_agent.load_contract_terms", return_value={}):
        result = await agent._get_observed_extreme("KXLOWTATL-26JUN03-B57.5", "2026-06-03", "temp_low")

    assert result["station_resolved"] is False
    assert result["realized_extreme"] is None
    agent._noaa.get_observed_extreme.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_observed_extreme_disabled_by_config():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._noaa = MagicMock()
    agent._noaa.get_observed_extreme = AsyncMock()
    with patch("kalshi_trader.agents.weather_agent.cfg.get", return_value=False):
        result = await agent._get_observed_extreme("KXLOWTATL", "2026-06-03", "temp_low", station_id="KATL")
    assert result["station_resolved"] is False
    agent._noaa.get_observed_extreme.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_ensemble_signal_handler_clamps_with_observation():
    agent = WeatherAgent.__new__(WeatherAgent)
    ensemble = {"members": [59.6] * 31, "member_count": 31}
    observation = {
        "station_id": "KATL", "timezone": "America/New_York",
        "realized_extreme": 57.2, "at_timestamp": "2026-06-03T09:50:00+00:00",
        "latest_timestamp": "2026-06-03T19:45:00+00:00", "lock_fraction": 1.0,
        "station_resolved": True,
    }
    result = await agent._build_ensemble_signal(
        ticker="KXLOWTATL-26JUN03-B57.5", metric="temp_low", threshold=57.0,
        operator="between", ensemble=ensemble, threshold_high=58.0, observation=observation,
    )
    assert result["metadata"]["members_clamped"] is True
    assert result["metadata"]["realized_extreme"] == pytest.approx(57.2)
    # lock_fraction read from the observation dict → uncertainty at the floor.
    assert result["uncertainty"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_close_closes_all_clients():
    agent = WeatherAgent.__new__(WeatherAgent)
    agent._noaa = MagicMock(); agent._noaa.close = AsyncMock()
    agent._open_meteo = MagicMock(); agent._open_meteo.close = AsyncMock()
    agent._x = MagicMock(); agent._x.close = AsyncMock()

    await agent.close()

    agent._noaa.close.assert_awaited_once()
    agent._open_meteo.close.assert_awaited_once()
    agent._x.close.assert_awaited_once()
