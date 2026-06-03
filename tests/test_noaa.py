import pytest
from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from kalshi_trader.external.noaa import (
    NOAAClient,
    _observation_station_candidates,
    _parse_wind_mph,
    _precip_to_inches,
)


def _station_metadata(timezone_name: str = "America/New_York") -> dict:
    return {"properties": {"timeZone": timezone_name}}


def _observation(timestamp: str, temperature_celsius=None, precip_mm=None) -> dict:
    properties: dict = {"timestamp": timestamp}
    if temperature_celsius is not None:
        properties["temperature"] = {"value": temperature_celsius, "unitCode": "wmoUnit:degC"}
    if precip_mm is not None:
        properties["precipitationLastHour"] = {"value": precip_mm, "unitCode": "wmoUnit:mm"}
    return {"properties": properties}


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


@pytest.mark.asyncio
async def test_get_discussion_empty_graph_returns_fallback():
    points_response = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
            "forecastHourly": "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
            "cwa": "OKX",
        }
    }
    products_response = {"@graph": []}
    client = NOAAClient()
    with patch.object(client, "_get", new=AsyncMock(side_effect=[points_response, products_response])):
        result = await client.get_discussion(40.7128, -74.0060)
    assert result["text"] == ""
    assert isinstance(result["issuance_time"], datetime)
    await client.close()


# ---------------------------------------------------------------------------
# get_observed_extreme (live-observation override input)
# ---------------------------------------------------------------------------

def test_observation_station_candidates_prefixes_three_letter_code():
    assert _observation_station_candidates("ATL") == ["KATL", "ATL"]
    assert _observation_station_candidates("KATL") == ["KATL"]
    assert _observation_station_candidates("") == []


def test_precip_to_inches_converts_millimeters():
    assert _precip_to_inches(25.4, "wmoUnit:mm") == pytest.approx(1.0)
    assert _precip_to_inches(0.0254, "wmoUnit:m") == pytest.approx(1.0, abs=1e-3)


@pytest.mark.asyncio
async def test_get_observed_extreme_temp_low_returns_min_in_window():
    # Atlanta local day (EDT, UTC-4): Jun 3 window is 04:00Z → Jun 4 04:00Z.
    observations = {
        "features": [
            _observation("2026-06-03T08:50:00+00:00", temperature_celsius=14.0),  # 57.2F (min)
            _observation("2026-06-03T12:00:00+00:00", temperature_celsius=20.0),
            _observation("2026-06-03T19:45:00+00:00", temperature_celsius=26.0),  # latest
        ]
    }
    client = NOAAClient()
    with patch.object(
        client, "_get",
        new=AsyncMock(side_effect=[_station_metadata("America/New_York"), observations]),
    ):
        result = await client.get_observed_extreme("ATL", date(2026, 6, 3), "temp_low")

    assert result["station_id"] == "KATL"
    assert result["realized_extreme"] == pytest.approx(57.2, abs=0.05)
    assert result["at_timestamp"] == "2026-06-03T08:50:00+00:00"
    assert result["obs_count"] == 3
    await client.close()


@pytest.mark.asyncio
async def test_get_observed_extreme_temp_high_returns_max_and_excludes_out_of_window():
    # Minneapolis local day (CDT, UTC-5): Jun 3 window is 05:00Z → Jun 4 05:00Z.
    observations = {
        "features": [
            _observation("2026-06-03T02:00:00+00:00", temperature_celsius=40.0),  # before window — excluded
            _observation("2026-06-03T17:00:00+00:00", temperature_celsius=28.0),  # 82.4F
            _observation("2026-06-03T18:50:00+00:00", temperature_celsius=29.0),  # 84.2F (max)
        ]
    }
    client = NOAAClient()
    with patch.object(
        client, "_get",
        new=AsyncMock(side_effect=[_station_metadata("America/Chicago"), observations]),
    ):
        result = await client.get_observed_extreme("MSP", date(2026, 6, 3), "temp_high")

    assert result["realized_extreme"] == pytest.approx(84.2, abs=0.05)  # not 104F from the excluded 40C
    assert result["obs_count"] == 2
    await client.close()


@pytest.mark.asyncio
async def test_get_observed_extreme_precipitation_sums_hourly():
    observations = {
        "features": [
            _observation("2026-06-03T10:00:00+00:00", precip_mm=12.7),  # 0.5 in
            _observation("2026-06-03T11:00:00+00:00", precip_mm=12.7),  # 0.5 in
        ]
    }
    client = NOAAClient()
    with patch.object(
        client, "_get",
        new=AsyncMock(side_effect=[_station_metadata("America/Chicago"), observations]),
    ):
        result = await client.get_observed_extreme("AUS", date(2026, 6, 3), "precipitation")

    assert result["realized_extreme"] == pytest.approx(1.0, abs=0.01)
    await client.close()


@pytest.mark.asyncio
async def test_get_observed_extreme_empty_observations_returns_none():
    client = NOAAClient()
    with patch.object(
        client, "_get",
        new=AsyncMock(side_effect=[_station_metadata("America/New_York"), {"features": []}]),
    ):
        result = await client.get_observed_extreme("ATL", date(2026, 6, 3), "temp_low")
    assert result["realized_extreme"] is None
    assert result["obs_count"] == 0
    await client.close()


@pytest.mark.asyncio
async def test_get_observed_extreme_no_timezone_returns_none_without_fetching_obs():
    metadata_without_timezone = {"properties": {}}
    get_mock = AsyncMock(side_effect=[metadata_without_timezone])
    client = NOAAClient()
    with patch.object(client, "_get", new=get_mock):
        result = await client.get_observed_extreme("KATL", date(2026, 6, 3), "temp_low")
    assert result["realized_extreme"] is None
    assert get_mock.await_count == 1  # never fetched observations
    await client.close()


@pytest.mark.asyncio
async def test_get_observed_extreme_falls_back_to_second_station_candidate():
    # First candidate (KATL) 404s; the bare code (ATL) resolves.
    observations = {"features": [_observation("2026-06-03T08:50:00+00:00", temperature_celsius=14.0)]}
    get_mock = AsyncMock(side_effect=[Exception("404"), _station_metadata("America/New_York"), observations])
    client = NOAAClient()
    with patch.object(client, "_get", new=get_mock):
        result = await client.get_observed_extreme("ATL", date(2026, 6, 3), "temp_low")
    assert result["station_id"] == "ATL"
    assert result["realized_extreme"] == pytest.approx(57.2, abs=0.05)
    await client.close()
