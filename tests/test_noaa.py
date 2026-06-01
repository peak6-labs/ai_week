import pytest
from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from kalshi_trader.external.noaa import NOAAClient, _parse_wind_mph


def test_parse_wind_mph_single():
    assert _parse_wind_mph("10 mph") == 10.0


def test_parse_wind_mph_range():
    assert _parse_wind_mph("10 to 15 mph") == 12.5


def test_parse_wind_mph_empty():
    assert _parse_wind_mph("") == 0.0


@pytest.mark.asyncio
async def test_get_forecast_returns_structured_data():
    points_response = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
            "forecastHourly": "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
            "cwa": "OKX",
        }
    }
    forecast_response = {
        "properties": {
            "generatedAt": "2026-06-01T12:00:00Z",
            "periods": [
                {
                    "isDaytime": True,
                    "startTime": "2026-06-03T06:00:00-05:00",
                    "temperature": 82,
                    "temperatureUnit": "F",
                    "windSpeed": "10 mph",
                    "shortForecast": "Sunny",
                    "probabilityOfPrecipitation": {"value": 20},
                },
                {
                    "isDaytime": False,
                    "startTime": "2026-06-03T18:00:00-05:00",
                    "temperature": 65,
                    "temperatureUnit": "F",
                    "windSpeed": "5 mph",
                    "shortForecast": "Clear",
                    "probabilityOfPrecipitation": {"value": 10},
                },
            ],
        }
    }

    client = NOAAClient()
    with patch.object(client, "_get", new=AsyncMock(side_effect=[points_response, forecast_response])):
        result = await client.get_forecast(40.7128, -74.0060, date(2026, 6, 3))

    assert result["temp_high"] == 82
    assert result["temp_low"] == 65
    assert result["precip_pct"] == 20
    assert result["wind_mph"] == 10.0
    assert result["short_forecast"] == "Sunny"
    assert isinstance(result["generated_at"], datetime)
    await client.close()


@pytest.mark.asyncio
async def test_get_discussion_returns_text_and_time():
    points_response = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
            "forecastHourly": "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
            "cwa": "OKX",
        }
    }
    products_response = {
        "@graph": [{"@id": "https://api.weather.gov/products/abc123"}]
    }
    product_response = {
        "productText": "High confidence in the forecast. Temperatures well-defined.",
        "issuanceTime": "2026-06-01T06:00:00Z",
    }
    client = NOAAClient()
    with patch.object(client, "_get", new=AsyncMock(side_effect=[points_response, products_response, product_response])):
        result = await client.get_discussion(40.7128, -74.0060)

    assert "confidence" in result["text"].lower()
    assert isinstance(result["issuance_time"], datetime)
    await client.close()
