"""Tests for PolymarketAgent — ties price comparison + whale watching together."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from kalshi_trader.agents.polymarket_agent import PolymarketAgent
from kalshi_trader.models import Market, Side


# --- Fixtures ---

def _make_kalshi_market(ticker="KTEST-1", title="Will Bitcoin close above $100k?",
                         yes_bid=48.0, yes_ask=52.0, open_interest=2000,
                         hours_to_close=24):
    return Market(
        ticker=ticker,
        event_ticker="KTEST",
        series_ticker="KTEST",
        title=title,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        last_price=50.0,
        volume_24h=5000,
        open_interest=open_interest,
        category="crypto",
        close_time=datetime.now(tz=timezone.utc) + timedelta(hours=hours_to_close),
        status="open",
    )


def _make_poly_market(question="Will Bitcoin close above $100k?",
                      yes_price="0.65", condition_id="0xcond1"):
    return {
        "id": "pm1",
        "question": question,
        "conditionId": condition_id,
        "outcomePrices": json.dumps([yes_price, str(1 - float(yes_price))]),
        "volume": "100000",
        "volume24hr": "8000",
        "updatedAt": "2026-06-01T12:00:00Z",
        "active": True,
        "closed": False,
    }


def _make_agent(target_wallets=None, poly_markets=None, large_trades=None):
    """Build a PolymarketAgent with its HTTP client fully mocked."""
    from kalshi_trader.external.polymarket import PolymarketClient
    client = PolymarketClient()
    client.get_markets = AsyncMock(return_value=poly_markets or [])
    client.get_large_trades = AsyncMock(return_value=large_trades or [])
    return PolymarketAgent(target_wallets=target_wallets or [], client=client)


# --- Core routing ---

@pytest.mark.asyncio
async def test_agent_generates_trade_idea_when_gap_exists():
    """Polymarket 65¢, Kalshi 50¢ — gap is 15¢, above 7¢ threshold."""
    agent = _make_agent(
        poly_markets=[_make_poly_market(yes_price="0.65")],
    )
    markets = [_make_kalshi_market(yes_bid=48.0, yes_ask=52.0)]
    ideas = await agent.run(markets)
    assert len(ideas) == 1


@pytest.mark.asyncio
async def test_agent_side_is_yes_when_polymarket_higher():
    """Polymarket thinks YES is more likely → we buy YES on Kalshi."""
    agent = _make_agent(poly_markets=[_make_poly_market(yes_price="0.72")])
    markets = [_make_kalshi_market(yes_bid=48.0, yes_ask=52.0)]
    ideas = await agent.run(markets)
    assert ideas[0].side == Side.YES


@pytest.mark.asyncio
async def test_agent_side_is_no_when_kalshi_higher():
    """Kalshi prices YES at 80¢, Polymarket at 55¢ — buy NO on Kalshi."""
    agent = _make_agent(poly_markets=[_make_poly_market(yes_price="0.55")])
    markets = [_make_kalshi_market(yes_bid=78.0, yes_ask=82.0)]
    ideas = await agent.run(markets)
    assert ideas[0].side == Side.NO


@pytest.mark.asyncio
async def test_agent_skips_market_when_no_polymarket_match():
    agent = _make_agent(poly_markets=[_make_poly_market(question="Will the Lakers win?")])
    markets = [_make_kalshi_market(title="Will Bitcoin close above $100k?")]
    ideas = await agent.run(markets)
    assert ideas == []


@pytest.mark.asyncio
async def test_agent_skips_market_when_gap_too_small():
    """2¢ gap is below the 7¢ minimum — no trade."""
    agent = _make_agent(poly_markets=[_make_poly_market(yes_price="0.52")])
    markets = [_make_kalshi_market(yes_bid=48.0, yes_ask=52.0)]
    ideas = await agent.run(markets)
    assert ideas == []


@pytest.mark.asyncio
async def test_agent_skips_market_with_low_open_interest():
    """Kalshi market with open_interest < 500 can't fill — skip."""
    agent = _make_agent(poly_markets=[_make_poly_market(yes_price="0.75")])
    markets = [_make_kalshi_market(yes_bid=48.0, yes_ask=52.0, open_interest=100)]
    ideas = await agent.run(markets)
    assert ideas == []


@pytest.mark.asyncio
async def test_agent_skips_market_closing_in_under_4_hours():
    agent = _make_agent(poly_markets=[_make_poly_market(yes_price="0.75")])
    markets = [_make_kalshi_market(yes_bid=48.0, yes_ask=52.0, hours_to_close=2)]
    ideas = await agent.run(markets)
    assert ideas == []


@pytest.mark.asyncio
async def test_agent_returns_empty_for_empty_input():
    agent = _make_agent()
    assert await agent.run([]) == []


# --- Trade idea fields ---

@pytest.mark.asyncio
async def test_agent_trade_idea_has_correct_ticker():
    agent = _make_agent(poly_markets=[_make_poly_market(yes_price="0.72")])
    markets = [_make_kalshi_market(ticker="BTC-100K-1")]
    ideas = await agent.run(markets)
    assert ideas[0].ticker == "BTC-100K-1"


@pytest.mark.asyncio
async def test_agent_signal_sources_includes_polymarket():
    agent = _make_agent(poly_markets=[_make_poly_market(yes_price="0.72")])
    ideas = await agent.run([_make_kalshi_market()])
    assert any("polymarket" in s for s in ideas[0].signal_sources)


@pytest.mark.asyncio
async def test_agent_confidence_between_0_and_1():
    agent = _make_agent(poly_markets=[_make_poly_market(yes_price="0.72")])
    ideas = await agent.run([_make_kalshi_market()])
    assert 0.0 < ideas[0].confidence <= 1.0


# --- Whale signal integration ---

@pytest.mark.asyncio
async def test_agent_boosts_confidence_with_agreeing_whale():
    """A whale entering in the same direction as our gap signal lifts confidence."""
    import time
    from kalshi_trader.external.polymarket import PolymarketClient
    client = PolymarketClient()
    # 10¢ gap → base_conf = 0.50, leaving room for the 0.15 whale boost
    client.get_markets = AsyncMock(return_value=[_make_poly_market(yes_price="0.60")])

    whale_trade = {
        "proxyWallet": "0xwhale1",
        "side": "BUY",
        "size": 1200.0,
        "price": 0.62,
        "timestamp": int(time.time()) - 60,
        "conditionId": "0xcond1",
        "title": "Will Bitcoin close above $100k?",
        "outcome": "Yes",
        "transactionHash": "0xtx",
    }
    client.get_large_trades = AsyncMock(return_value=[whale_trade])

    agent_no_whale = _make_agent(poly_markets=[_make_poly_market(yes_price="0.60")])
    agent_whale = PolymarketAgent(
        target_wallets=["0xwhale1"],
        client=client,
    )

    ideas_no_whale = await agent_no_whale.run([_make_kalshi_market()])
    ideas_whale = await agent_whale.run([_make_kalshi_market()])

    assert ideas_whale[0].confidence > ideas_no_whale[0].confidence


@pytest.mark.asyncio
async def test_agent_whale_signal_source_listed_when_target_enters():
    import time
    from kalshi_trader.external.polymarket import PolymarketClient
    client = PolymarketClient()
    client.get_markets = AsyncMock(return_value=[_make_poly_market(yes_price="0.72")])
    client.get_large_trades = AsyncMock(return_value=[{
        "proxyWallet": "0xwhale1",
        "side": "BUY", "size": 1200.0, "price": 0.62,
        "timestamp": int(time.time()) - 60,
        "conditionId": "0xcond1",
        "title": "Will Bitcoin close above $100k?",
        "outcome": "Yes", "transactionHash": "0xtx",
    }])
    agent = PolymarketAgent(target_wallets=["0xwhale1"], client=client)
    ideas = await agent.run([_make_kalshi_market()])
    assert any("whale" in s for s in ideas[0].signal_sources)


@pytest.mark.asyncio
async def test_agent_ignores_non_target_whale():
    """Large trade from a wallet NOT in target_wallets doesn't boost confidence."""
    import time
    from kalshi_trader.external.polymarket import PolymarketClient
    client = PolymarketClient()
    client.get_markets = AsyncMock(return_value=[_make_poly_market(yes_price="0.72")])
    client.get_large_trades = AsyncMock(return_value=[{
        "proxyWallet": "0xstranger",
        "side": "BUY", "size": 1200.0, "price": 0.62,
        "timestamp": int(time.time()) - 60,
        "conditionId": "0xcond1",
        "title": "Will Bitcoin close above $100k?",
        "outcome": "Yes", "transactionHash": "0xtx",
    }])

    agent_no_target = _make_agent(
        target_wallets=[],
        poly_markets=[_make_poly_market(yes_price="0.72")],
    )
    # patch the get_large_trades on the no-target agent too
    agent_no_target._client.get_large_trades = AsyncMock(return_value=[{
        "proxyWallet": "0xstranger",
        "side": "BUY", "size": 1200.0, "price": 0.62,
        "timestamp": int(time.time()) - 60,
        "conditionId": "0xcond1",
        "title": "Will Bitcoin close above $100k?",
        "outcome": "Yes", "transactionHash": "0xtx",
    }])
    agent_with_target = PolymarketAgent(target_wallets=["0xwhale1"], client=client)

    ideas_no = await agent_no_target.run([_make_kalshi_market()])
    ideas_with = await agent_with_target.run([_make_kalshi_market()])

    # non-target whale should not change confidence
    assert ideas_no[0].confidence == pytest.approx(ideas_with[0].confidence)
