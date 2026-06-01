"""Tests for Polymarket whale copy-trading signal detection.

Strategy (LunarResearcher): identify large traders who consistently enter
early on winning positions, signal when they enter, exit on volume spike.
"""
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi_trader.external.polymarket import PolymarketClient, WhaleSignal


# --- Fixtures ---

def _make_trade(wallet="0xabc", side="BUY", size=1000.0, price=0.42,
                ts=None, condition_id="0xcond1", title="Will X happen?"):
    return {
        "proxyWallet": wallet,
        "side": side,
        "size": size,
        "price": price,
        "timestamp": ts or int(time.time()),
        "conditionId": condition_id,
        "title": title,
        "outcome": "Yes",
        "transactionHash": "0xtxhash",
    }


def _make_position(wallet="0xabc", condition_id="0xcond1",
                   title="Will X happen?", size=100,
                   avg_price=0.42, cur_price=0.65,
                   cash_pnl=23.0, percent_pnl=54.8):
    return {
        "proxyWallet": wallet,
        "conditionId": condition_id,
        "title": title,
        "size": size,
        "avgPrice": avg_price,
        "curPrice": cur_price,
        "cashPnl": cash_pnl,
        "percentPnl": percent_pnl,
        "outcome": "Yes",
    }


# --- detect_whale_entries ---

def test_detect_whale_entries_returns_signal_for_large_buy():
    client = PolymarketClient()
    trades = [_make_trade(size=800.0, side="BUY")]
    signals = client.detect_whale_entries(trades, min_size_usd=500.0)
    assert len(signals) == 1
    assert signals[0].side == "YES"
    assert signals[0].size_usd == 800.0


def test_detect_whale_entries_ignores_small_trades():
    client = PolymarketClient()
    trades = [_make_trade(size=50.0, side="BUY")]
    signals = client.detect_whale_entries(trades, min_size_usd=500.0)
    assert signals == []


def test_detect_whale_entries_ignores_sell_side():
    """Whales exiting is noise — we copy entries, not exits."""
    client = PolymarketClient()
    trades = [_make_trade(size=2000.0, side="SELL")]
    signals = client.detect_whale_entries(trades, min_size_usd=500.0)
    assert signals == []


def test_detect_whale_entries_filters_old_trades():
    """Only trades within lookback window count as fresh signals."""
    client = PolymarketClient()
    old_ts = int(time.time()) - 7200  # 2 hours ago
    trades = [_make_trade(size=1000.0, side="BUY", ts=old_ts)]
    signals = client.detect_whale_entries(trades, min_size_usd=500.0, lookback_seconds=3600)
    assert signals == []


def test_detect_whale_entries_accepts_recent_trades():
    client = PolymarketClient()
    recent_ts = int(time.time()) - 300  # 5 minutes ago
    trades = [_make_trade(size=1000.0, side="BUY", ts=recent_ts)]
    signals = client.detect_whale_entries(trades, min_size_usd=500.0, lookback_seconds=3600)
    assert len(signals) == 1


def test_detect_whale_entries_populates_signal_fields():
    client = PolymarketClient()
    trades = [_make_trade(
        wallet="0xwhale", size=1500.0, price=0.38,
        condition_id="0xcond99", title="Will it rain?",
    )]
    signals = client.detect_whale_entries(trades, min_size_usd=500.0)
    s = signals[0]
    assert s.wallet_address == "0xwhale"
    assert s.condition_id == "0xcond99"
    assert s.market_question == "Will it rain?"
    assert s.entry_price == pytest.approx(0.38)
    assert s.size_usd == pytest.approx(1500.0)


def test_detect_whale_entries_maps_no_outcome_to_no_side():
    """Trades on the NO outcome should produce side='NO'."""
    client = PolymarketClient()
    trade = _make_trade(size=800.0, side="BUY")
    trade["outcome"] = "No"
    signals = client.detect_whale_entries([trade], min_size_usd=500.0)
    assert signals[0].side == "NO"


# --- score_wallet_profitability ---

def test_score_wallet_all_profitable_positions():
    client = PolymarketClient()
    positions = [
        _make_position(cash_pnl=50.0),
        _make_position(cash_pnl=30.0),
    ]
    score = client.score_wallet_profitability(positions)
    assert score == pytest.approx(1.0)


def test_score_wallet_all_losing_positions():
    client = PolymarketClient()
    positions = [
        _make_position(cash_pnl=-20.0),
        _make_position(cash_pnl=-10.0),
    ]
    score = client.score_wallet_profitability(positions)
    assert score == pytest.approx(0.0)


def test_score_wallet_mixed_positions():
    client = PolymarketClient()
    positions = [
        _make_position(cash_pnl=40.0),   # win
        _make_position(cash_pnl=-10.0),  # loss
        _make_position(cash_pnl=20.0),   # win
        _make_position(cash_pnl=-5.0),   # loss
    ]
    score = client.score_wallet_profitability(positions)
    assert score == pytest.approx(0.5)  # 2 wins / 4 total


def test_score_wallet_empty_positions_returns_zero():
    client = PolymarketClient()
    assert client.score_wallet_profitability([]) == pytest.approx(0.0)


# --- get_large_trades (async, HTTP mocked) ---

@pytest.mark.asyncio
async def test_get_large_trades_calls_data_api():
    raw = [
        _make_trade(size=2000.0),
        _make_trade(size=100.0),   # below threshold
        _make_trade(size=800.0),
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
        trades = await client.get_large_trades("0xcond1", min_size_usd=500.0)

    assert len(trades) == 2
    assert all(t["size"] >= 500.0 for t in trades)


# --- get_wallet_positions (async, HTTP mocked) ---

@pytest.mark.asyncio
async def test_get_wallet_positions_returns_position_list():
    raw = [_make_position(), _make_position(condition_id="0xcond2")]
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
        positions = await client.get_wallet_positions("0xabc")

    assert len(positions) == 2
    assert positions[0]["proxyWallet"] == "0xabc"
