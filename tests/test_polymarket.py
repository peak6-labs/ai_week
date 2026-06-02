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


# --- match_market: numeric guard ---

def test_match_market_rejects_different_numeric_threshold():
    """Core false-positive fix: $120k vs $1m are different events despite sharing 'bitcoin hit'."""
    client = PolymarketClient()
    poly_markets = [_make_gamma_market(id="n1", question="Will bitcoin hit $1m before GTA VI?")]
    result = client.match_market("Will Bitcoin hit $120k?", poly_markets)
    assert result is None


def test_match_market_accepts_same_numeric_threshold():
    """Same threshold and shared content tokens → valid match."""
    client = PolymarketClient()
    poly_markets = [_make_gamma_market(id="n2", question="Will Bitcoin close above $100k in June?")]
    result = client.match_market("Will Bitcoin close above $100k in July?", poly_markets)
    assert result is not None


def test_match_market_k_suffix_normalised_to_thousands():
    """$100k and $100,000 should be treated as the same number."""
    client = PolymarketClient()
    poly_markets = [_make_gamma_market(id="n3", question="Will Bitcoin close above $100,000?")]
    result = client.match_market("Will Bitcoin close above $100k?", poly_markets)
    assert result is not None


def test_match_market_allows_match_when_neither_title_has_numbers():
    """No numbers in either title → numeric guard does not fire."""
    client = PolymarketClient()
    poly_markets = [_make_gamma_market(id="n4", question="Will Jerome Powell resign from the Fed?")]
    result = client.match_market("Will Powell step down as Fed chair?", poly_markets)
    assert result is not None


def test_match_market_stopwords_alone_do_not_match():
    """Titles sharing only stopwords (will, the, a, in, by) should not match."""
    client = PolymarketClient()
    poly_markets = [_make_gamma_market(id="n5", question="Will the deal close by Friday?")]
    result = client.match_market("Will the Lakers win by Sunday?", poly_markets)
    assert result is None


# --- match_market_with_score ---

def test_match_market_with_score_returns_score():
    client = PolymarketClient()
    poly_markets = [
        _make_gamma_market(id="0xabc", question="Will the Boston Celtics win the NBA championship?"),
    ]
    result = client.match_market_with_score(
        "Will the Boston Celtics win the 2026 NBA Finals?", poly_markets
    )
    assert result is not None
    match, score = result
    assert match["id"] == "0xabc"
    assert 0.0 < score <= 1.0


def test_match_market_with_score_no_match_returns_none():
    client = PolymarketClient()
    poly_markets = [
        _make_gamma_market(id="0xdef", question="Will it rain in Seattle tomorrow?"),
    ]
    result = client.match_market_with_score("Will the Lakers win the championship?", poly_markets)
    assert result is None


# --- detect_volume_spike ---

def test_detect_volume_spike_true_when_current_exceeds_3x_average():
    client = PolymarketClient()
    recent = [1000, 1200, 900, 1100, 1050]  # avg ~1050, 3x = ~3150
    assert client.detect_volume_spike(current=3200, recent_volumes=recent) is True


def test_detect_volume_spike_false_below_3x():
    client = PolymarketClient()
    recent = [1000, 1200, 900, 1100, 1050]  # avg ~1050, 3x = ~3150
    assert client.detect_volume_spike(current=3000, recent_volumes=recent) is False


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
    """Keyset endpoint returns {markets: [...], next_cursor: null} — single page."""
    raw = {"markets": [_make_gamma_market(id="m1"), _make_gamma_market(id="m2")], "next_cursor": None}
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
    raw = {
        "markets": [
            _make_gamma_market(id="active1", active=True, closed=False),
            _make_gamma_market(id="closed1", active=False, closed=True),
        ],
        "next_cursor": None,
    }
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
