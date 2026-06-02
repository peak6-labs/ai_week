"""Tests for kalshi_trader/signals/polymarket.py"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from kalshi_trader.signals.polymarket import build_price_signal, build_whale_signal


# ---------------------------------------------------------------------------
# build_price_signal
# ---------------------------------------------------------------------------

def test_build_price_signal_basic():
    sig = build_price_signal(
        ticker="KALSHI-XYZ",
        poly_prob=0.45,
        gap_cents=18.0,
        match_score=0.91,
    )
    assert sig.source == "polymarket_price"
    assert sig.weight == pytest.approx(0.75)
    assert sig.uncertainty == pytest.approx(0.03)
    assert sig.probability == pytest.approx(0.45)
    assert sig.metadata["data_quality"] == "fresh"
    assert sig.metadata["gap_cents"] == pytest.approx(18.0)
    assert sig.metadata["match_score"] == pytest.approx(0.91, abs=0.0001)
    assert sig.metadata["ticker"] == "KALSHI-XYZ"
    assert isinstance(sig.metadata["narrative"], str)
    assert len(sig.metadata["narrative"]) > 0


def test_build_price_signal_negative_gap():
    sig = build_price_signal(
        ticker="KALSHI-XYZ",
        poly_prob=0.30,
        gap_cents=-8.0,
        match_score=0.85,
    )
    assert sig.metadata["gap_cents"] == pytest.approx(-8.0)


def test_build_price_signal_custom_fetched_at():
    ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    sig = build_price_signal(
        ticker="KALSHI-XYZ",
        poly_prob=0.55,
        gap_cents=5.0,
        match_score=0.80,
        fetched_at=ts,
    )
    assert sig.data_issued_at == ts


# ---------------------------------------------------------------------------
# build_whale_signal
# ---------------------------------------------------------------------------

def test_build_whale_signal_yes_entries():
    entries = [
        {
            "wallet_address": "0xAAA",
            "side": "YES",
            "entry_price": 0.62,
            "size_usd": 2000,
            "timestamp": datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
        },
        {
            "wallet_address": "0xBBB",
            "side": "YES",
            "entry_price": 0.58,
            "size_usd": 1000,
            "timestamp": datetime(2025, 6, 1, 11, 0, 0, tzinfo=timezone.utc),
        },
    ]
    sig = build_whale_signal(ticker="KALSHI-XYZ", whale_entries=entries)
    assert sig is not None
    # weighted avg: (2000 * 0.62 + 1000 * 0.58) / 3000 = (1240 + 580) / 3000 = 0.6067
    assert sig.probability == pytest.approx(0.6067, abs=0.001)
    assert sig.uncertainty == pytest.approx(0.10)
    assert sig.metadata["whale_count"] == 2
    assert sig.source == "polymarket_whale"
    assert sig.weight == pytest.approx(0.60)


def test_build_whale_signal_no_entries():
    result = build_whale_signal(ticker="KALSHI-XYZ", whale_entries=[])
    assert result is None


def test_build_whale_signal_single_whale_higher_uncertainty():
    entries = [
        {
            "wallet_address": "0xAAA",
            "side": "YES",
            "entry_price": 0.70,
            "size_usd": 5000,
            "timestamp": datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
        },
    ]
    sig = build_whale_signal(ticker="KALSHI-XYZ", whale_entries=entries)
    assert sig is not None
    assert sig.uncertainty == pytest.approx(0.15)


def test_build_whale_signal_no_side_entry():
    # NO entry at price=0.60 → implied YES prob = 1 - 0.60 = 0.40
    entries = [
        {
            "wallet_address": "0xAAA",
            "side": "NO",
            "entry_price": 0.60,
            "size_usd": 3000,
            "timestamp": datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
        },
    ]
    sig = build_whale_signal(ticker="KALSHI-XYZ", whale_entries=entries)
    assert sig is not None
    assert sig.probability == pytest.approx(0.40)


def test_build_whale_signal_iso_timestamp():
    # timestamp as ISO string
    entries = [
        {
            "wallet_address": "0xAAA",
            "side": "YES",
            "entry_price": 0.65,
            "size_usd": 1000,
            "timestamp": "2025-06-01T10:00:00+00:00",
        },
    ]
    sig = build_whale_signal(ticker="KALSHI-XYZ", whale_entries=entries)
    assert sig is not None
    assert sig.data_issued_at.tzinfo is not None


def test_build_whale_signal_most_recent_timestamp():
    # data_issued_at should be the most recent timestamp
    entries = [
        {
            "wallet_address": "0xAAA",
            "side": "YES",
            "entry_price": 0.65,
            "size_usd": 1000,
            "timestamp": datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
        },
        {
            "wallet_address": "0xBBB",
            "side": "YES",
            "entry_price": 0.70,
            "size_usd": 1000,
            "timestamp": datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        },
    ]
    sig = build_whale_signal(ticker="KALSHI-XYZ", whale_entries=entries)
    assert sig is not None
    expected = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert sig.data_issued_at == expected
