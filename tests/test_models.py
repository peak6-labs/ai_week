from datetime import datetime
from kalshi_trader.models import (
    Side, OrderAction,
    Market, TradeIdea, RiskDecision, OrderResult, Position, PortfolioState, RankedSlate,
)


def test_side_enum():
    assert Side.YES == "yes"
    assert Side.NO == "no"


def test_market_dataclass():
    m = Market(
        ticker="TEST-TICKER", event_ticker="TEST-EVENT", series_ticker="TEST",
        title="Test market", yes_bid=22.0, yes_ask=24.0, last_price=23.0,
        volume_24h=1000, open_interest=500, category="sports",
        close_time=datetime(2026, 6, 5), status="open",
    )
    assert m.ticker == "TEST-TICKER"
    assert m.yes_bid == 22.0


def test_trade_idea_defaults():
    idea = TradeIdea(
        agent_id="conditional_event", ticker="X", side=Side.YES,
        action=OrderAction.BUY, confidence=0.45, market_price=22.0,
        reasoning="test", signal_sources=["A1"],
    )
    assert idea.suggested_size_dollars == 0.0
    assert idea.category == ""


def test_portfolio_state_defaults():
    state = PortfolioState(balance_dollars=500.0)
    assert state.daily_realized_pnl == 0.0
    assert state.total_exposure_dollars == 0.0
    assert state.exposure_by_category == {}


def test_exit_signal_fields():
    from kalshi_trader.models import ExitSignal
    signal = ExitSignal(reason="stop_loss", exit_price_cents=34.0, description="down 30% from cost basis")
    assert signal.reason == "stop_loss"
    assert signal.exit_price_cents == 34.0
    assert signal.description == "down 30% from cost basis"
