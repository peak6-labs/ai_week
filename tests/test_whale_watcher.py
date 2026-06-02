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


# ---------------------------------------------------------------------------
# bootstrap_whale_targets (async, methods mocked via AsyncMock side_effect)
# ---------------------------------------------------------------------------

def _make_mock_client(markets, trades_by_call, positions_by_wallet):
    """Return a PolymarketClient whose async IO methods are fully mocked.

    Args:
        markets: list of market dicts returned by get_markets
        trades_by_call: list of lists — each inner list is the trades returned
                        for the corresponding sequential get_large_trades call
        positions_by_wallet: dict mapping wallet address -> list of position dicts
    """
    client = PolymarketClient()

    # get_markets always returns the same single list
    client.get_markets = AsyncMock(return_value=markets)

    # get_large_trades is called once per market — side_effect drives sequence
    client.get_large_trades = AsyncMock(side_effect=trades_by_call)

    # get_wallet_positions is called once per unique wallet
    async def _positions(address, limit=100):
        return positions_by_wallet.get(address, [])

    client.get_wallet_positions = AsyncMock(side_effect=_positions)

    return client


def _make_market(condition_id):
    return {"conditionId": condition_id, "active": True, "closed": False,
            "question": f"Will market {condition_id} resolve Yes?"}


@pytest.mark.asyncio
async def test_bootstrap_returns_only_wallets_above_min_score():
    """Wallets whose profitability score falls below min_score are excluded."""
    markets = [_make_market("0xcond1")]
    # Two wallets appear in trades for this market
    trades = [
        _make_trade(wallet="0xgood", size=600.0, condition_id="0xcond1"),
        _make_trade(wallet="0xbad", size=600.0, condition_id="0xcond1"),
    ]
    # good wallet: 2 winning positions → score 1.0 (above 0.6)
    good_positions = [_make_position(cash_pnl=10.0), _make_position(cash_pnl=5.0)]
    # bad wallet: 2 losing positions → score 0.0 (below 0.6)
    bad_positions = [_make_position(cash_pnl=-10.0), _make_position(cash_pnl=-5.0)]

    client = _make_mock_client(
        markets=markets,
        trades_by_call=[trades],
        positions_by_wallet={"0xgood": good_positions, "0xbad": bad_positions},
    )

    result = await client.bootstrap_whale_targets(min_score=0.6)

    assert "0xgood" in result
    assert "0xbad" not in result


@pytest.mark.asyncio
async def test_bootstrap_deduplicates_wallets_across_markets():
    """A wallet appearing in multiple markets is only scored once."""
    markets = [_make_market("0xcond1"), _make_market("0xcond2")]
    # Same wallet appears in both markets
    trades_market1 = [_make_trade(wallet="0xshared", size=600.0, condition_id="0xcond1")]
    trades_market2 = [_make_trade(wallet="0xshared", size=700.0, condition_id="0xcond2")]

    positions = [_make_position(cash_pnl=20.0), _make_position(cash_pnl=10.0)]

    client = _make_mock_client(
        markets=markets,
        trades_by_call=[trades_market1, trades_market2],
        positions_by_wallet={"0xshared": positions},
    )

    result = await client.bootstrap_whale_targets(min_score=0.6)

    # Appears once in result, get_wallet_positions called exactly once
    assert result.count("0xshared") == 1
    client.get_wallet_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_respects_top_n_limit():
    """Only the top N wallets by score are returned."""
    markets = [_make_market("0xcond1")]
    # Three wallets, all above min_score
    trades = [
        _make_trade(wallet="0xw1", size=600.0),
        _make_trade(wallet="0xw2", size=600.0),
        _make_trade(wallet="0xw3", size=600.0),
    ]
    # w1: 3/3 wins (score 1.0), w2: 2/3 wins (score ~0.67), w3: 1/1 win (score 1.0)
    positions_by_wallet = {
        "0xw1": [_make_position(cash_pnl=10.0)] * 3,
        "0xw2": [_make_position(cash_pnl=10.0), _make_position(cash_pnl=10.0),
                 _make_position(cash_pnl=-5.0)],
        "0xw3": [_make_position(cash_pnl=10.0)],
    }

    client = _make_mock_client(
        markets=markets,
        trades_by_call=[trades],
        positions_by_wallet=positions_by_wallet,
    )

    result = await client.bootstrap_whale_targets(min_score=0.6, top_n=2)

    assert len(result) == 2
    # The lowest-scoring qualifying wallet (0xw2, score ~0.67) is dropped
    assert "0xw2" not in result


@pytest.mark.asyncio
async def test_bootstrap_returns_empty_when_no_qualifying_wallets():
    """Returns an empty list when no wallet meets the min_score threshold."""
    markets = [_make_market("0xcond1")]
    trades = [_make_trade(wallet="0xloser", size=600.0, condition_id="0xcond1")]
    losing_positions = [_make_position(cash_pnl=-10.0), _make_position(cash_pnl=-5.0)]

    client = _make_mock_client(
        markets=markets,
        trades_by_call=[trades],
        positions_by_wallet={"0xloser": losing_positions},
    )

    result = await client.bootstrap_whale_targets(min_score=0.6)

    assert result == []
