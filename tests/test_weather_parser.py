import pytest
from datetime import date
from kalshi_trader.external.weather_parser import parse_title, parse_discussion


def test_parse_title_temp_above():
    result = parse_title("WEATHER-NYC-HIGH-JUNE3", "NYC high temp June 3: above 80°F?")
    assert result is not None
    assert result["city"] == "nyc"
    assert result["metric"] == "temp_high"
    assert result["threshold"] == 80.0
    assert result["operator"] == "above"
    assert result["target_date"] == "2026-06-03"
    assert result["lat"] == pytest.approx(40.7128)


def test_parse_title_rain():
    result = parse_title("WEATHER-CHI-RAIN-JUNE4", "Will it rain in Chicago on June 4?")
    assert result is not None
    assert result["metric"] == "precipitation"
    assert result["city"] == "chicago"


def test_parse_title_below():
    result = parse_title("TICKER", "Will Denver high temp be below 90°F on June 5?")
    assert result is not None
    assert result["operator"] == "below"
    assert result["threshold"] == 90.0


def test_parse_title_unknown_city_returns_none():
    result = parse_title("TICKER", "Will it rain in Timbuktu on June 3?")
    assert result is None


def test_parse_title_no_threshold_returns_none():
    result = parse_title("TICKER", "NYC high temp June 3")
    assert result is None


def test_parse_title_no_date_returns_none():
    result = parse_title("TICKER", "NYC high temp above 80°F")
    assert result is None


def test_parse_discussion_high_confidence():
    text = "High confidence in the forecast. Temperatures well-defined for the period."
    result = parse_discussion(text)
    assert result["confidence"] == "high"
    assert isinstance(result["key_points"], list)


def test_parse_discussion_low_confidence():
    text = (
        "Uncertain timing on the cold front. Possible rain Thursday. "
        "Confidence is low with potential for significant uncertainty. "
        "The system may shift north or south. Could bring heavy rain. "
        "Uncertain about wind speeds."
    )
    result = parse_discussion(text)
    assert result["confidence"] == "low"
    assert len(result["key_points"]) > 0


def test_parse_discussion_medium_confidence():
    text = "Some uncertainty remains about exact amounts. Mostly clear skies expected."
    result = parse_discussion(text)
    assert result["confidence"] == "medium"
