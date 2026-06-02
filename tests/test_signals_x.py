"""Tests for kalshi_trader/signals/x.py"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from kalshi_trader.signals.x import build_x_signal


def test_build_x_signal_basic():
    raw_signal = {
        "source": "x_social",
        "probability": 0.62,
        "uncertainty": 0.12,
        "weight": 0.55,
        "data_issued_at": "2025-06-01T10:00:00+00:00",
    }
    sig = build_x_signal(
        ticker="KALSHI-XYZ",
        raw_signal=raw_signal,
        narrative="Bullish sentiment on X for this event.",
        sentiment_direction="bullish",
        sentiment_reasoning="Multiple high-follower accounts bullish.",
        strategies_used=["hashtag_volume", "influencer_scan"],
        post_count=142,
    )
    assert sig.source == "x_social"
    assert sig.probability == pytest.approx(0.62)
    assert sig.uncertainty == pytest.approx(0.12)
    assert sig.weight == pytest.approx(0.55)
    assert sig.data_issued_at == datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert sig.metadata["ticker"] == "KALSHI-XYZ"
    assert sig.metadata["narrative"] == "Bullish sentiment on X for this event."
    assert sig.metadata["sentiment_direction"] == "bullish"
    assert sig.metadata["sentiment_reasoning"] == "Multiple high-follower accounts bullish."
    assert sig.metadata["strategies_used"] == ["hashtag_volume", "influencer_scan"]
    assert sig.metadata["post_count"] == 142
    assert sig.metadata["data_quality"] == "fresh"


def test_build_x_signal_missing_issued_at_defaults_to_now():
    raw_signal = {
        "source": "x_social",
        "probability": 0.50,
        "uncertainty": 0.15,
        "weight": 0.50,
        # no data_issued_at
    }
    before = datetime.now(tz=timezone.utc)
    sig = build_x_signal(
        ticker="KALSHI-XYZ",
        raw_signal=raw_signal,
        narrative="No timestamp test.",
        sentiment_direction="neutral",
        sentiment_reasoning="Mixed signals.",
        strategies_used=[],
        post_count=0,
    )
    after = datetime.now(tz=timezone.utc)
    assert sig.data_issued_at.tzinfo is not None
    assert before <= sig.data_issued_at <= after


def test_build_x_signal_datetime_issued_at():
    # data_issued_at already a datetime object (not a string)
    ts = datetime(2025, 5, 15, 8, 30, 0, tzinfo=timezone.utc)
    raw_signal = {
        "source": "x_social",
        "probability": 0.45,
        "uncertainty": 0.10,
        "weight": 0.60,
        "data_issued_at": ts,
    }
    sig = build_x_signal(
        ticker="KALSHI-ABC",
        raw_signal=raw_signal,
        narrative="Test with datetime object.",
        sentiment_direction="bearish",
        sentiment_reasoning="Negative posts dominate.",
        strategies_used=["keyword_scan"],
        post_count=55,
    )
    assert sig.data_issued_at == ts
