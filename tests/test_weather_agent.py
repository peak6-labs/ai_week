import pytest
from datetime import datetime, timedelta
from kalshi_trader.agents.weather_agent import (
    _parse_weather_market,
    _estimate_probability,
    _combine_signals,
    _calculate_edge,
)


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


@pytest.mark.asyncio
async def test_estimate_probability_temp_above():
    # mean=(90+75)/2=82.5, std=(90-75)/4=3.75 → P(X>80) ≈ 0.748
    forecast = {"temp_high": 90.0, "temp_low": 75.0, "precip_pct": 10, "data_age_minutes": 30}
    result = await _estimate_probability(
        metric="temp_high", threshold=80.0, operator="above", forecast=forecast
    )
    assert "probability" in result
    assert 0.6 < result["probability"] < 0.9
    assert result["source"] == "noaa_gfs"
    assert "data_issued_at" in result


@pytest.mark.asyncio
async def test_estimate_probability_precip():
    forecast = {"temp_high": 75.0, "temp_low": 60.0, "precip_pct": 70, "data_age_minutes": 60}
    result = await _estimate_probability(
        metric="precipitation", threshold=0, operator="above", forecast=forecast
    )
    assert result["probability"] == pytest.approx(0.70)
    assert result["uncertainty"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_combine_signals_weighted_average():
    now = datetime.utcnow()
    estimates = [
        {
            "source": "noaa_gfs",
            "probability": 0.70,
            "uncertainty": 0.08,
            "weight": 0.85,
            "data_issued_at": (now - timedelta(minutes=30)).isoformat(),
        },
        {
            "source": "noaa_gfs_2",
            "probability": 0.60,
            "uncertainty": 0.10,
            "weight": 0.70,
            "data_issued_at": (now - timedelta(minutes=120)).isoformat(),
        },
    ]
    result = await _combine_signals(estimates=estimates)
    assert 0.60 < result["combined_probability"] < 0.70
    assert result["n_sources"] == 2
    assert "uncertainty" in result


@pytest.mark.asyncio
async def test_combine_signals_single():
    now = datetime.utcnow()
    estimates = [{
        "source": "noaa_gfs",
        "probability": 0.65,
        "uncertainty": 0.08,
        "weight": 0.85,
        "data_issued_at": (now - timedelta(minutes=10)).isoformat(),
    }]
    result = await _combine_signals(estimates=estimates)
    assert result["combined_probability"] == pytest.approx(0.65, abs=0.01)


@pytest.mark.asyncio
async def test_calculate_edge_worth_trading():
    result = await _calculate_edge(combined_probability=0.65, market_price_cents=40.0)
    assert result["edge_cents"] == pytest.approx(25.0)
    assert result["worth_trading"] is True


@pytest.mark.asyncio
async def test_calculate_edge_not_worth_trading():
    result = await _calculate_edge(combined_probability=0.42, market_price_cents=40.0)
    assert result["fee_adjusted_edge"] < 5.0
    assert result["worth_trading"] is False
