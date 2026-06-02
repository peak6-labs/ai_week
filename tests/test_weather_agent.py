import pytest
from kalshi_trader.agents.weather_agent import _parse_weather_market


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
    from kalshi_trader.agents.weather_agent import WeatherAgent
    agent = WeatherAgent.__new__(WeatherAgent)
    assert agent._parse_estimates("```json\n[]\n```") == []
