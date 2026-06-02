"""Tests for kalshi_trader/agents/parsing.py"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from kalshi_trader.agents.parsing import estimate_to_dict, parse_signal_estimates
from kalshi_trader.models import SignalEstimate


# ---------------------------------------------------------------------------
# parse_signal_estimates
# ---------------------------------------------------------------------------

def test_parse_signal_estimates_valid():
    raw = """\
Here is the analysis.

```json
[
  {
    "source": "noaa_gfs",
    "probability": 0.73,
    "uncertainty": 0.05,
    "weight": 0.85,
    "data_issued_at": "2025-06-01T10:00:00+00:00",
    "metadata": {"ticker": "WEATHER-NYC"}
  }
]
```
"""
    results = parse_signal_estimates(raw)
    assert len(results) == 1
    sig = results[0]
    assert isinstance(sig, SignalEstimate)
    assert sig.source == "noaa_gfs"
    assert sig.probability == pytest.approx(0.73)
    assert sig.uncertainty == pytest.approx(0.05)
    assert sig.weight == pytest.approx(0.85)
    assert sig.data_issued_at == datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert sig.metadata == {"ticker": "WEATHER-NYC"}


def test_parse_signal_estimates_empty_list():
    raw = "```json\n[]\n```"
    results = parse_signal_estimates(raw)
    assert results == []


def test_parse_signal_estimates_no_json_block():
    raw = "no json here, just text"
    results = parse_signal_estimates(raw)
    assert results == []


def test_parse_signal_estimates_bad_json():
    raw = "```json\n{broken\n```"
    results = parse_signal_estimates(raw)
    assert results == []


def test_parse_signal_estimates_skips_invalid_items():
    raw = """\
```json
[
  {
    "source": "noaa_gfs",
    "probability": 0.60,
    "uncertainty": 0.08,
    "weight": 0.85,
    "data_issued_at": "2025-06-01T10:00:00+00:00",
    "metadata": {}
  },
  {
    "missing_source_field": true,
    "probability": 0.50
  }
]
```"""
    results = parse_signal_estimates(raw)
    assert len(results) == 1
    assert results[0].source == "noaa_gfs"


def test_parse_signal_estimates_timezone_naive_iso_string():
    # timezone-naive ISO string should still parse without error
    raw = """\
```json
[
  {
    "source": "noaa_gfs",
    "probability": 0.55,
    "uncertainty": 0.08,
    "weight": 0.85,
    "data_issued_at": "2025-06-01T10:00:00",
    "metadata": {}
  }
]
```"""
    results = parse_signal_estimates(raw)
    assert len(results) == 1
    assert results[0].source == "noaa_gfs"
    # data_issued_at should be timezone-aware after parsing
    assert results[0].data_issued_at.tzinfo is not None


# ---------------------------------------------------------------------------
# estimate_to_dict
# ---------------------------------------------------------------------------

def test_estimate_to_dict_roundtrip():
    issued = datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    sig = SignalEstimate(
        source="polymarket_price",
        probability=0.65,
        uncertainty=0.03,
        weight=0.75,
        data_issued_at=issued,
        metadata={"ticker": "KALSHI-XYZ", "gap_cents": 12.0},
    )
    d = estimate_to_dict(sig)
    assert d["source"] == "polymarket_price"
    assert d["probability"] == pytest.approx(0.65)
    assert d["uncertainty"] == pytest.approx(0.03)
    assert d["weight"] == pytest.approx(0.75)
    assert d["data_issued_at"] == "2025-06-01T10:00:00+00:00"
    assert d["metadata"] == {"ticker": "KALSHI-XYZ", "gap_cents": 12.0}


def test_estimate_to_dict_all_fields_present():
    issued = datetime(2025, 1, 15, 8, 30, 0, tzinfo=timezone.utc)
    sig = SignalEstimate(
        source="x_social",
        probability=0.40,
        uncertainty=0.12,
        weight=0.55,
        data_issued_at=issued,
        metadata={},
    )
    d = estimate_to_dict(sig)
    for key in ("source", "probability", "uncertainty", "weight", "data_issued_at", "metadata"):
        assert key in d
