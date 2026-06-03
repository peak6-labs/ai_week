import pytest
from datetime import datetime, timezone

from kalshi_trader.external.gdelt import parse_timeline_json, extract_points
from kalshi_trader.signals.mentions import build_mentions_signal


# --- gdelt response parsing (no network) -----------------------------------

_SAMPLE_TIMELINE = """
{"query_details": {"title": "shutdown station:CSPAN"},
 "timeline": [ { "series": "CSPAN", "data": [
   { "date": "20240101T120000Z", "value": 0.5 },
   { "date": "20240201T120000Z", "value": 0.0 },
   { "date": "20240301T120000Z", "value": 1.2 } ] } ] }
"""


def test_parse_timeline_json_valid():
    parsed = parse_timeline_json(_SAMPLE_TIMELINE)
    assert "timeline" in parsed
    points = extract_points(parsed, "CSPAN")
    assert len(points) == 3
    assert points[0]["value"] == pytest.approx(0.5)


def test_parse_timeline_json_empty_object():
    assert parse_timeline_json("{}") == {}
    assert extract_points({}, "CSPAN") == []


def test_parse_timeline_json_error_string():
    # GDELT returns plain text on a malformed query.
    assert parse_timeline_json("Your query must contain at least one station.") == {}


def test_extract_points_single_unlabeled_series():
    timeline = {"timeline": [{"data": [{"date": "20240101T120000Z", "value": 2.0}]}]}
    points = extract_points(timeline, "CSPAN")
    assert len(points) == 1
    assert points[0]["value"] == pytest.approx(2.0)


# --- signal builder --------------------------------------------------------

def test_build_mentions_signal_basic():
    base_rate = {
        "period_count": 30,
        "periods_with_mention": 21,
        "fraction_with_mention": 0.7,
        "mean_match_percent": 0.4,
        "max_match_percent": 2.1,
    }
    sig = build_mentions_signal(
        ticker="KXMENTION-POWELL-RECESSION",
        phrase="recession",
        station="CSPAN",
        base_rate=base_rate,
        speaker="Jerome Powell",
    )
    assert sig.source == "gdelt_mentions"
    assert sig.probability == pytest.approx(0.7)
    assert sig.weight == pytest.approx(0.55)
    assert sig.uncertainty == pytest.approx(0.18)
    assert sig.metadata["data_quality"] == "fresh"
    assert sig.metadata["speaker"] == "Jerome Powell"
    assert sig.metadata["station"] == "CSPAN"
    assert isinstance(sig.metadata["narrative"], str) and sig.metadata["narrative"]
    assert sig.data_issued_at.tzinfo is not None


def test_build_mentions_signal_probability_clamped_low():
    base_rate = {
        "period_count": 30,
        "periods_with_mention": 0,
        "fraction_with_mention": 0.0,
        "mean_match_percent": 0.0,
        "max_match_percent": 0.0,
    }
    sig = build_mentions_signal("T", "shutdown", "CSPAN", base_rate)
    assert sig.probability >= 0.01


def test_build_mentions_signal_probability_clamped_high():
    base_rate = {
        "period_count": 30,
        "periods_with_mention": 30,
        "fraction_with_mention": 1.0,
        "mean_match_percent": 3.0,
        "max_match_percent": 5.0,
    }
    sig = build_mentions_signal("T", "shutdown", "CSPAN", base_rate)
    assert sig.probability <= 0.99


def test_build_mentions_signal_data_quality_thresholds():
    fresh = build_mentions_signal("T", "w", "CSPAN", {"period_count": 24, "fraction_with_mention": 0.3})
    stale = build_mentions_signal("T", "w", "CSPAN", {"period_count": 6, "fraction_with_mention": 0.3})
    unavailable = build_mentions_signal("T", "w", "CSPAN", {"period_count": 3, "fraction_with_mention": 0.3})
    assert fresh.metadata["data_quality"] == "fresh"
    assert stale.metadata["data_quality"] == "stale"
    assert unavailable.metadata["data_quality"] == "unavailable"
