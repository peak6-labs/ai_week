import pytest

# DISABLED for this session — the user turned off all trade execution. The
# TradeExecutor module is a hard-disabled guard (kalshi_trader/executor.py), so
# this whole test module is skipped at collection time and never runs. Do NOT
# re-enable until the user says it is okay. To restore: remove the skip below.
pytest.skip("executor disabled for this session — no execution allowed",
            allow_module_level=True)

import asyncio  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402
from datetime import datetime  # noqa: E402
from kalshi_trader.executor import TradeExecutor  # noqa: E402
from kalshi_trader.risk import RiskManager  # noqa: E402
from kalshi_trader.models import TradeIdea, RiskDecision, Side, OrderAction  # noqa: E402


@pytest.fixture
def risk():
    return RiskManager()


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.create_order.return_value = {
        "order": {
            "order_id": "ord-demo-123",
            "status": "executed",
            "yes_price": 22,
            "count": 2,
        }
    }
    return client


@pytest.fixture
def idea():
    return TradeIdea(
        agent_id="conditional_event",
        ticker="NBA-CELTICS-WIN",
        side=Side.YES,
        action=OrderAction.BUY,
        confidence=0.45,
        market_price=22.0,
        reasoning="test",
        signal_sources=[],
        category="sports",
    )


@pytest.fixture
def approved_decision():
    return RiskDecision(approved=True, approved_size_dollars=44.0)


@pytest.mark.asyncio
async def test_executor_places_order(mock_client, risk, idea, approved_decision):
    executor = TradeExecutor(mock_client, risk)
    result = await executor.execute(idea, approved_decision)
    assert result.order_id == "ord-demo-123"
    assert result.status == "executed"
    mock_client.create_order.assert_called_once()


@pytest.mark.asyncio
async def test_executor_rejects_unapproved(mock_client, risk, idea):
    executor = TradeExecutor(mock_client, risk)
    bad = RiskDecision(approved=False, approved_size_dollars=0, rejection_reason="no edge")
    with pytest.raises(ValueError, match="not approved"):
        await executor.execute(idea, bad)


@pytest.mark.asyncio
async def test_executor_contract_count(mock_client, risk, idea, approved_decision):
    executor = TradeExecutor(mock_client, risk)
    await executor.execute(idea, approved_decision)
    # $44 at $0.22/contract = 200 contracts
    call_kwargs = mock_client.create_order.call_args
    assert call_kwargs.kwargs["count"] == 200


@pytest.mark.asyncio
async def test_executor_no_side_price_flip(mock_client, risk, approved_decision):
    no_idea = TradeIdea(
        agent_id="flow_volume", ticker="NBA-CELTICS-WIN", side=Side.NO,
        action=OrderAction.BUY, confidence=0.60, market_price=22.0,
        reasoning="test", signal_sources=[], category="sports",
    )
    executor = TradeExecutor(mock_client, risk)
    await executor.execute(no_idea, approved_decision)
    call_kwargs = mock_client.create_order.call_args
    # NO side: yes_price should be 100 - 22 = 78
    assert call_kwargs.kwargs["yes_price"] == 78
    assert call_kwargs.kwargs["side"] == "no"


# --- Live demo integration test (skipped when balance is zero) ---

@pytest.mark.asyncio
async def test_live_demo_trade():
    """Places a real $10 order on the demo account — opt-in only.

    This is the one test in the suite that actually *executes* a trade. It is
    gated behind KALSHI_ALLOW_LIVE_DEMO_TRADE=1 so a normal ``pytest tests/`` run
    never sends an order (and never fails on demo-API connectivity). Set the env
    var deliberately when you want to exercise the live demo path.
    """
    import os
    if os.environ.get("KALSHI_ALLOW_LIVE_DEMO_TRADE") != "1":
        pytest.skip("live demo trade disabled — set KALSHI_ALLOW_LIVE_DEMO_TRADE=1 to opt in")
    try:
        from kalshi_trader.client import KalshiClient
        from kalshi_trader.scanner import MarketScanner
        client = KalshiClient()
        bal = await client.get_balance()
        balance_dollars = float(bal.get("balance_dollars", "0"))
        if balance_dollars < 10:
            pytest.skip(f"Demo balance ${balance_dollars:.2f} — fund at demo.kalshi.co to enable live test")

        scanner = MarketScanner(client)
        risk = RiskManager()
        executor = TradeExecutor(client, risk)

        # Find a liquid market
        resp = await client.get_markets(status="open", limit=200)
        tradeable = [
            m for m in resp.get("markets", [])
            if 0.10 <= float(m.get("yes_bid_dollars", 0)) <= 0.40
        ]
        if not tradeable:
            pytest.skip("No liquid markets found on demo right now")

        m = tradeable[0]
        price_cents = float(m["yes_bid_dollars"]) * 100

        idea = TradeIdea(
            agent_id="live_test",
            ticker=m["ticker"],
            side=Side.YES,
            action=OrderAction.BUY,
            confidence=price_cents / 100 + 0.10,
            market_price=price_cents,
            reasoning="live demo test",
            signal_sources=["test"],
            category=m.get("event_ticker", "")[:20],
        )
        from kalshi_trader.models import PortfolioState
        portfolio_state = PortfolioState(balance_dollars=balance_dollars)
        decision = risk.check_trade(idea, portfolio_state)
        if not decision.approved:
            pytest.skip(f"Risk check blocked trade: {decision.rejection_reason}")

        result = await executor.execute(idea, decision)
        print(f"\nLive trade result: {result.ticker} {result.side.value} "
              f"x{result.size_dollars/price_cents*100:.0f} contracts @ ${result.fill_price/100:.2f}  "
              f"order_id={result.order_id}")
        assert result.order_id
        assert result.status in ("executed", "resting", "pending")
    except Exception as e:
        if "balance" in str(e).lower() or "insufficient" in str(e).lower():
            pytest.skip(f"Insufficient demo funds: {e}")
        raise
