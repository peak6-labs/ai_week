"""Tests for ExitMonitor — detects when to close open positions."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from kalshi_trader.exit_monitor import ExitMonitor, TradeEntry
from kalshi_trader.models import Market, Side


def _make_kalshi_market(ticker="BTC-1", title="Will Bitcoin close above $100k?",
                         yes_bid=58.0, yes_ask=62.0, hours_to_close=12):
    return Market(
        ticker=ticker,
        event_ticker="BTC",
        series_ticker="BTC",
        title=title,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        last_price=60.0,
        volume_24h=5000,
        open_interest=2000,
        category="crypto",
        close_time=datetime.now(tz=timezone.utc) + timedelta(hours=hours_to_close),
        status="open",
    )


def _make_poly_market(question="Will Bitcoin close above $100k?", volume_24hr="8000"):
    return {
        "conditionId": "0xcond1",
        "question": question,
        "volume24hr": volume_24hr,
        "active": True,
        "closed": False,
        "outcomePrices": "[0.60, 0.40]",
        "updatedAt": "2026-06-01T12:00:00Z",
        "volume": "100000",
    }


def _make_trade_entry(
    ticker="BTC-1",
    side=Side.YES,
    entry_price_prob=0.50,   # entered at 50¢
    entry_gap=0.15,           # 15¢ gap at entry
    hours_ago=1.0,
    entry_volume_24h=4000.0,
    condition_id="0xcond1",
):
    return TradeEntry(
        ticker=ticker,
        condition_id=condition_id,
        side=side,
        entry_price_prob=entry_price_prob,
        entry_gap=entry_gap,
        entry_time=datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago),
        entry_volume_24h=entry_volume_24h,
    )


def _make_monitor(poly_markets=None):
    from kalshi_trader.external.polymarket import PolymarketClient
    client = PolymarketClient()
    client.get_markets = AsyncMock(return_value=poly_markets or [_make_poly_market()])
    return ExitMonitor(poly_client=client)


# --- take_profit ---

@pytest.mark.asyncio
async def test_take_profit_triggered_when_price_converged():
    """Price moved 90% of entry gap (above 85% threshold) → take_profit."""
    # Entry: 50¢ prob, gap 15¢. Target = 50 + 15*0.85 = 62.75¢ prob.
    # Current Kalshi: bid=64, ask=68 → midpoint 66¢ → 0.66 (above target).
    monitor = _make_monitor()
    trade = _make_trade_entry(entry_price_prob=0.50, entry_gap=0.15)
    markets = [_make_kalshi_market(yes_bid=64.0, yes_ask=68.0)]

    exits = await monitor.check_exits([trade], markets)
    assert len(exits) == 1
    assert exits[0][1] == "take_profit"


@pytest.mark.asyncio
async def test_no_exit_when_price_not_converged_enough():
    """Price moved only 50% of gap — below 85% threshold."""
    # Entry: 50¢, gap 15¢. Target = 62.75¢. Current = 57.5¢ (50% of gap).
    monitor = _make_monitor()
    trade = _make_trade_entry(entry_price_prob=0.50, entry_gap=0.15, hours_ago=1.0)
    markets = [_make_kalshi_market(yes_bid=56.0, yes_ask=59.0)]  # mid=57.5¢

    exits = await monitor.check_exits([trade], markets)
    assert exits == []


# --- stale_thesis ---

@pytest.mark.asyncio
async def test_stale_thesis_triggered_after_24h_no_move():
    """Position held 30h with <2% price change → stale."""
    monitor = _make_monitor()
    trade = _make_trade_entry(
        entry_price_prob=0.50, entry_gap=0.15, hours_ago=30.0
    )
    # Current price 51¢ → abs change 1¢ < 2¢ threshold
    markets = [_make_kalshi_market(yes_bid=50.0, yes_ask=52.0)]

    exits = await monitor.check_exits([trade], markets)
    assert len(exits) == 1
    assert exits[0][1] == "stale_thesis"


@pytest.mark.asyncio
async def test_stale_thesis_not_triggered_within_24h():
    monitor = _make_monitor()
    trade = _make_trade_entry(entry_price_prob=0.50, entry_gap=0.15, hours_ago=20.0)
    markets = [_make_kalshi_market(yes_bid=50.0, yes_ask=52.0)]

    exits = await monitor.check_exits([trade], markets)
    assert exits == []


# --- volume_spike ---

@pytest.mark.asyncio
async def test_volume_spike_triggers_exit():
    """Polymarket volume 3× the entry baseline → exit."""
    poly_markets = [_make_poly_market(volume_24hr="12001")]  # >2× 4000 baseline
    monitor = _make_monitor(poly_markets=poly_markets)
    # Price hasn't moved enough for take_profit; not stale yet
    trade = _make_trade_entry(
        entry_price_prob=0.50, entry_gap=0.15, hours_ago=2.0,
        entry_volume_24h=4000.0
    )
    markets = [_make_kalshi_market(yes_bid=55.0, yes_ask=57.0)]  # mid=56 (not at take_profit)

    exits = await monitor.check_exits([trade], markets)
    assert len(exits) == 1
    assert exits[0][1] == "volume_spike"


@pytest.mark.asyncio
async def test_volume_spike_not_triggered_within_normal_range():
    poly_markets = [_make_poly_market(volume_24hr="5000")]  # 1.25× baseline — not a spike
    monitor = _make_monitor(poly_markets=poly_markets)
    trade = _make_trade_entry(
        entry_price_prob=0.50, entry_gap=0.15, hours_ago=2.0,
        entry_volume_24h=4000.0
    )
    markets = [_make_kalshi_market(yes_bid=55.0, yes_ask=57.0)]

    exits = await monitor.check_exits([trade], markets)
    assert exits == []


@pytest.mark.asyncio
async def test_volume_spike_skipped_when_no_entry_baseline():
    """If entry_volume_24h == 0, skip volume spike check entirely."""
    poly_markets = [_make_poly_market(volume_24hr="99999")]
    monitor = _make_monitor(poly_markets=poly_markets)
    trade = _make_trade_entry(
        entry_price_prob=0.50, entry_gap=0.15, hours_ago=2.0,
        entry_volume_24h=0.0  # no baseline stored
    )
    markets = [_make_kalshi_market(yes_bid=55.0, yes_ask=57.0)]

    exits = await monitor.check_exits([trade], markets)
    assert exits == []


# --- Ordering: take_profit wins over stale ---

@pytest.mark.asyncio
async def test_take_profit_wins_over_stale_thesis():
    """When both conditions are met, report take_profit (first check wins)."""
    monitor = _make_monitor()
    trade = _make_trade_entry(
        entry_price_prob=0.50, entry_gap=0.15, hours_ago=30.0
    )
    # Price at 66¢ → both take_profit (>62.75¢) AND stale would be true
    markets = [_make_kalshi_market(yes_bid=64.0, yes_ask=68.0)]

    exits = await monitor.check_exits([trade], markets)
    assert exits[0][1] == "take_profit"


# --- Edge cases ---

@pytest.mark.asyncio
async def test_unknown_ticker_skipped():
    """If the ticker isn't in the kalshi_markets list, skip it silently."""
    monitor = _make_monitor()
    trade = _make_trade_entry(ticker="UNKNOWN-99")
    markets = [_make_kalshi_market(ticker="BTC-1")]

    exits = await monitor.check_exits([trade], markets)
    assert exits == []


@pytest.mark.asyncio
async def test_empty_open_trades_returns_empty():
    monitor = _make_monitor()
    exits = await monitor.check_exits([], [_make_kalshi_market()])
    assert exits == []


@pytest.mark.asyncio
async def test_no_exit_side_is_preserved_in_result():
    monitor = _make_monitor()
    trade = _make_trade_entry(entry_price_prob=0.50, entry_gap=0.15)
    markets = [_make_kalshi_market(yes_bid=64.0, yes_ask=68.0)]

    exits = await monitor.check_exits([trade], markets)
    assert exits[0][0].ticker == "BTC-1"
