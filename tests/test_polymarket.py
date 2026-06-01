"""Tests for Polymarket Gamma API client and signal generation."""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi_trader.external.polymarket import PolymarketClient
from kalshi_trader.models import SignalEstimate


# --- Fixtures ---

def _make_gamma_market(**overrides):
    base = {
        "id": "abc123",
        "question": "Will BTC close above $100k on June 30 2026?",
        "outcomePrices": json.dumps(["0.72", "0.28"]),
        "volume": "125000",
        "volume24hr": "8500",
        "endDate": "2026-06-30T00:00:00Z",
        "updatedAt": "2026-06-01T12:00:00Z",
        "active": True,
        "closed": False,
    }
    base.update(overrides)
    return base


# --- to_signal_estimate ---

def test_to_signal_estimate_probability_from_yes_price():
    client = PolymarketClient()
    market = _make_gamma_market()  # outcomePrices[0] = "0.72"
    sig = client.to_signal_estimate(market)
    assert sig.probability == pytest.approx(0.72)


def test_to_signal_estimate_source_is_polymarket():
    client = PolymarketClient()
    sig = client.to_signal_estimate(_make_gamma_market())
    assert sig.source == "polymarket"


def test_to_signal_estimate_weight_is_0_75():
    client = PolymarketClient()
    sig = client.to_signal_estimate(_make_gamma_market())
    assert sig.weight == pytest.approx(0.75)


def test_to_signal_estimate_uncertainty_higher_near_50_pct():
    """Markets near 50¢ are more uncertain than near-resolved markets."""
    client = PolymarketClient()
    near_even = client.to_signal_estimate(_make_gamma_market(outcomePrices=json.dumps(["0.50", "0.50"])))
    near_resolved = client.to_signal_estimate(_make_gamma_market(outcomePrices=json.dumps(["0.95", "0.05"])))
    assert near_even.uncertainty > near_resolved.uncertainty


def test_to_signal_estimate_data_issued_at_from_updatedAt():
    client = PolymarketClient()
    sig = client.to_signal_estimate(_make_gamma_market(updatedAt="2026-06-01T10:30:00Z"))
    assert sig.data_issued_at == datetime(2026, 6, 1, 10, 30, 0, tzinfo=timezone.utc)


def test_to_signal_estimate_metadata_includes_volume():
    client = PolymarketClient()
    sig = client.to_signal_estimate(_make_gamma_market(volume24hr="8500"))
    assert sig.metadata["volume_24h"] == 8500


# --- match_market ---

def test_match_market_returns_best_title_match():
    client = PolymarketClient()
    poly_markets = [
        _make_gamma_market(id="x1", question="Will Jerome Powell resign in 2026?"),
        _make_gamma_market(id="x2", question="Will the Fed cut rates in June 2026?"),
        _make_gamma_market(id="x3", question="Will inflation fall below 3% in 2026?"),
    ]
    result = client.match_market("Fed rate cut June 2026", poly_markets)
    assert result is not None
    assert result["id"] == "x2"


def test_match_market_returns_none_below_threshold():
    client = PolymarketClient()
    poly_markets = [
        _make_gamma_market(id="y1", question="Will the Lakers win the NBA Finals?"),
    ]
    result = client.match_market("Will it rain in Seattle on June 15?", poly_markets)
    assert result is None


def test_match_market_case_insensitive():
    client = PolymarketClient()
    poly_markets = [_make_gamma_market(id="z1", question="WILL BTC CLOSE ABOVE $100K?")]
    result = client.match_market("will btc close above $100k?", poly_markets)
    assert result is not None


# --- detect_volume_spike ---

def test_detect_volume_spike_true_when_current_exceeds_2x_average():
    client = PolymarketClient()
    recent = [1000, 1200, 900, 1100, 1050]  # avg ~1050
    assert client.detect_volume_spike(current=3000, recent_volumes=recent) is True


def test_detect_volume_spike_false_for_normal_volume():
    client = PolymarketClient()
    recent = [1000, 1200, 900, 1100, 1050]
    assert client.detect_volume_spike(current=1300, recent_volumes=recent) is False


def test_detect_volume_spike_false_with_empty_history():
    client = PolymarketClient()
    assert client.detect_volume_spike(current=9999, recent_volumes=[]) is False


# --- get_markets (async, HTTP mocked) ---

@pytest.mark.asyncio
async def test_get_markets_returns_parsed_list():
    raw = [_make_gamma_market(id="m1"), _make_gamma_market(id="m2")]
    mock_response = AsyncMock()
    mock_response.json = AsyncMock(return_value=raw)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("kalshi_trader.external.polymarket.aiohttp.ClientSession", return_value=mock_session):
        client = PolymarketClient()
        markets = await client.get_markets()

    assert len(markets) == 2
    assert markets[0]["id"] == "m1"


@pytest.mark.asyncio
async def test_get_markets_filters_inactive():
    raw = [
        _make_gamma_market(id="active1", active=True, closed=False),
        _make_gamma_market(id="closed1", active=False, closed=True),
    ]
    mock_response = AsyncMock()
    mock_response.json = AsyncMock(return_value=raw)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("kalshi_trader.external.polymarket.aiohttp.ClientSession", return_value=mock_session):
        client = PolymarketClient()
        markets = await client.get_markets()

    assert len(markets) == 1
    assert markets[0]["id"] == "active1"
