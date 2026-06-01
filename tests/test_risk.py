import pytest
from datetime import datetime, timedelta
from kalshi_trader.models import TradeIdea, Side, OrderAction, PortfolioState
from kalshi_trader.risk import RiskManager


@pytest.fixture
def risk():
    return RiskManager()


@pytest.fixture
def empty_portfolio():
    return PortfolioState(balance_dollars=500.0)


@pytest.fixture
def sample_idea():
    return TradeIdea(
        agent_id="conditional_event",
        ticker="NBA-CELTICS-WIN",
        side=Side.YES,
        action=OrderAction.BUY,
        confidence=0.45,
        market_price=22.0,
        reasoning="Underpriced",
        signal_sources=["A1"],
        category="sports",
    )


def test_approved_trade(risk, sample_idea, empty_portfolio):
    decision = risk.check_trade(sample_idea, empty_portfolio)
    assert decision.approved
    assert 10.0 <= decision.approved_size_dollars <= 100.0


def test_reject_below_minimum_edge(risk, empty_portfolio):
    idea = TradeIdea(
        agent_id="conditional_event", ticker="X", side=Side.YES,
        action=OrderAction.BUY, confidence=0.23, market_price=22.0,
        reasoning="", signal_sources=[], category="sports",
    )
    decision = risk.check_trade(idea, empty_portfolio)
    assert not decision.approved
    assert "edge" in decision.rejection_reason.lower()


def test_reject_exceeds_max_exposure(risk, sample_idea):
    portfolio = PortfolioState(
        balance_dollars=500.0,
        total_exposure_dollars=401.0,
    )
    decision = risk.check_trade(sample_idea, portfolio)
    assert not decision.approved
    assert "exposure" in decision.rejection_reason.lower()


def test_reject_exceeds_category_limit(risk, sample_idea):
    portfolio = PortfolioState(
        balance_dollars=500.0,
        exposure_by_category={"sports": 251.0},
    )
    decision = risk.check_trade(sample_idea, portfolio)
    assert not decision.approved
    assert "category" in decision.rejection_reason.lower()


def test_reject_settlement_too_soon(risk, sample_idea, empty_portfolio):
    decision = risk.check_trade(
        sample_idea, empty_portfolio,
        close_time=datetime.utcnow() + timedelta(hours=1),
    )
    assert not decision.approved
    assert "settlement" in decision.rejection_reason.lower()


def test_settlement_far_enough_away_is_approved(risk, sample_idea, empty_portfolio):
    decision = risk.check_trade(
        sample_idea, empty_portfolio,
        close_time=datetime.utcnow() + timedelta(hours=3),
    )
    assert decision.approved


def test_half_kelly_capped_at_max(risk, empty_portfolio):
    # High confidence + cheap price → uncapped Kelly would exceed $100
    idea = TradeIdea(
        agent_id="conditional_event", ticker="X", side=Side.YES,
        action=OrderAction.BUY, confidence=0.60, market_price=30.0,
        reasoning="", signal_sources=[], category="sports",
    )
    decision = risk.check_trade(idea, empty_portfolio)
    assert decision.approved
    assert decision.approved_size_dollars <= 100.0


def test_daily_loss_limit_blocks_trading(risk, sample_idea):
    portfolio = PortfolioState(
        balance_dollars=400.0,
        daily_realized_pnl=-100.0,
    )
    decision = risk.check_trade(sample_idea, portfolio)
    assert not decision.approved
    assert "daily loss" in decision.rejection_reason.lower()


def test_fee_calculation(risk):
    # 0.07 * 0.50 * 0.50 * (100 / 0.50) contracts = $3.50
    fee = risk.estimate_fee_dollars(price_cents=50.0, size_dollars=100.0)
    assert abs(fee - 3.50) < 0.01


def test_adaptive_sizing_reduces_after_three_losses(risk, sample_idea, empty_portfolio):
    risk.record_loss("sports")
    risk.record_loss("sports")
    risk.record_loss("sports")
    reduced = risk.check_trade(sample_idea, empty_portfolio)

    risk2 = RiskManager()
    full = risk2.check_trade(sample_idea, empty_portfolio)

    assert reduced.approved
    assert reduced.approved_size_dollars < full.approved_size_dollars


def test_win_streak_restores_sizing(risk, sample_idea, empty_portfolio):
    for _ in range(3):
        risk.record_loss("sports")
    for _ in range(3):
        risk.record_win("sports")

    restored = risk.check_trade(sample_idea, empty_portfolio)
    risk2 = RiskManager()
    full = risk2.check_trade(sample_idea, empty_portfolio)

    assert restored.approved_size_dollars == full.approved_size_dollars
