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


# --- current Kalshi format: city in TICKER, "<NN" threshold, symbol operator ---

def test_parse_title_min_temp_below_symbol_city_from_ticker():
    # Real cycle-4 market: city only in the ticker, "minimum temperature", "<45".
    result = parse_title("KXLOWTBOS-26JUN03-T45", "Will the minimum temperature be <45° on Jun 3, 2026?")
    assert result is not None
    assert result["city"] == "boston"
    assert result["metric"] == "temp_low"
    assert result["operator"] == "below"
    assert result["threshold"] == 45.0
    assert result["target_date"] == "2026-06-03"


def test_parse_title_min_temp_above_symbol():
    result = parse_title("KXLOWTLV-26JUN03-T75", "Will the minimum temperature be >75° on Jun 3, 2026?")
    assert result is not None
    assert result["city"] == "las vegas"
    assert result["metric"] == "temp_low"
    assert result["operator"] == "above"
    assert result["threshold"] == 75.0


def test_parse_title_san_francisco_from_ticker():
    result = parse_title("KXLOWTSFO-26JUN03-T48", "Will the minimum temperature be <48° on Jun 3, 2026?")
    assert result is not None
    assert result["city"] == "san francisco"
    assert result["lat"] == pytest.approx(37.7749)


def test_parse_title_max_temperature_is_temp_high():
    result = parse_title("KXHIGHTNYC-26JUN03-T90", "Will the maximum temperature be >90° on Jun 3, 2026?")
    assert result is not None
    assert result["metric"] == "temp_high"
    assert result["operator"] == "above"
    assert result["threshold"] == 90.0


def test_parse_title_explicit_year_in_title_is_used():
    result = parse_title("KXLOWTDAL-27JAN02-T40", "Will the minimum temperature be <40° on Jan 2, 2027?")
    assert result is not None
    assert result["target_date"] == "2027-01-02"


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
