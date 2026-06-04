import pytest
from datetime import datetime, timedelta
from kalshi_trader.models import TradeIdea, Side, OrderAction, PortfolioState
from kalshi_trader.risk import (
    RiskManager,
    GENERAL_TAKER_FEE_COEFFICIENT,
    INDEX_MARKET_FEE_COEFFICIENT,
)


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
    # General taker: 0.07 * (100 / 0.50) contracts * 0.50 * 0.50 = $3.50
    fee = risk.estimate_fee_dollars(price_cents=50.0, size_dollars=100.0)
    assert abs(fee - 3.50) < 0.01


def test_fee_taker_rounds_up_to_next_cent(risk):
    # 0.07 * (20 / 0.37) * 0.37 * 0.63 = $0.882, which Kalshi rounds up to $0.89.
    fee = risk.estimate_fee_dollars(price_cents=37.0, size_dollars=20.0)
    assert fee == pytest.approx(0.89)


def test_fee_maker_is_free_on_general_markets(risk):
    fee = risk.estimate_fee_dollars(price_cents=50.0, size_dollars=100.0, is_maker=True)
    assert fee == 0.0


def test_fee_index_market_uses_reduced_coefficient(risk):
    # Reduced 0.035 taker coefficient → half the general taker fee.
    fee = risk.estimate_fee_dollars(price_cents=50.0, size_dollars=100.0, is_index_market=True)
    assert fee == pytest.approx(1.75)


def test_fee_index_market_charges_maker_fee(risk):
    # Unlike general markets, index-market makers are charged (also 0.035).
    fee = risk.estimate_fee_dollars(
        price_cents=50.0, size_dollars=100.0, is_maker=True, is_index_market=True
    )
    assert fee == pytest.approx(1.75)


def test_fee_zero_at_price_extremes(risk):
    assert risk.estimate_fee_dollars(price_cents=0.0, size_dollars=100.0) == 0.0
    assert risk.estimate_fee_dollars(price_cents=100.0, size_dollars=100.0) == 0.0


# --- Robust fee invariants ---------------------------------------------------
# Swept across a spread of prices (including near the 1¢/99¢ extremes) and sizes.
FEE_PRICE_CENTS_SWEEP = [1.0, 5.0, 10.0, 17.0, 25.0, 33.0, 37.0, 50.0, 63.0, 75.0, 88.0, 95.0, 99.0]
FEE_SIZE_DOLLARS_SWEEP = [10.0, 20.0, 44.0, 50.0, 87.5, 100.0]


@pytest.mark.parametrize("price_cents", FEE_PRICE_CENTS_SWEEP)
@pytest.mark.parametrize("size_dollars", FEE_SIZE_DOLLARS_SWEEP)
def test_fee_taker_is_always_a_whole_cent_rounded_up(risk, price_cents, size_dollars):
    fee = risk.estimate_fee_dollars(price_cents, size_dollars)
    exact_fee_dollars = GENERAL_TAKER_FEE_COEFFICIENT * size_dollars * (1.0 - price_cents / 100.0)
    # Always non-negative and quantized to whole cents.
    assert fee >= 0.0
    assert abs(fee * 100.0 - round(fee * 100.0)) < 1e-6
    # Rounded *up*: never undercharges, never overshoots by a full cent or more.
    assert fee >= exact_fee_dollars - 1e-9
    assert fee <= exact_fee_dollars + 0.01 + 1e-9


@pytest.mark.parametrize("price_cents", FEE_PRICE_CENTS_SWEEP)
@pytest.mark.parametrize("size_dollars", FEE_SIZE_DOLLARS_SWEEP)
def test_fee_maker_is_free_on_general_markets_across_sweep(risk, price_cents, size_dollars):
    assert risk.estimate_fee_dollars(price_cents, size_dollars, is_maker=True) == 0.0


@pytest.mark.parametrize("price_cents", FEE_PRICE_CENTS_SWEEP)
@pytest.mark.parametrize("size_dollars", FEE_SIZE_DOLLARS_SWEEP)
def test_index_fee_obeys_reduced_coefficient_and_never_exceeds_taker(risk, price_cents, size_dollars):
    index_fee = risk.estimate_fee_dollars(price_cents, size_dollars, is_index_market=True)
    general_taker_fee = risk.estimate_fee_dollars(price_cents, size_dollars)
    exact_index_fee_dollars = INDEX_MARKET_FEE_COEFFICIENT * size_dollars * (1.0 - price_cents / 100.0)
    assert abs(index_fee * 100.0 - round(index_fee * 100.0)) < 1e-6
    assert index_fee >= exact_index_fee_dollars - 1e-9
    assert index_fee <= exact_index_fee_dollars + 0.01 + 1e-9
    # The reduced coefficient never costs more than the general taker rate.
    assert index_fee <= general_taker_fee + 1e-9
    # Index markets charge makers the same reduced rate as takers.
    index_maker_fee = risk.estimate_fee_dollars(
        price_cents, size_dollars, is_maker=True, is_index_market=True
    )
    assert index_maker_fee == index_fee


@pytest.mark.parametrize(
    "price_cents,expected_fee_dollars",
    [(10.0, 6.30), (25.0, 5.25), (50.0, 3.50), (75.0, 1.75), (90.0, 0.70)],
)
def test_fee_exact_values_for_100_dollar_taker(risk, price_cents, expected_fee_dollars):
    # Pins the current published schedule for a $100 general taker order. Several
    # of these involve floating-point products that would mis-round without the
    # round-then-ceil guard (e.g. $100 @ 50¢ computes to 350.0000000000001¢).
    fee = risk.estimate_fee_dollars(price_cents=price_cents, size_dollars=100.0)
    assert fee == pytest.approx(expected_fee_dollars)


def test_fee_non_decreasing_in_size(risk):
    fees = [risk.estimate_fee_dollars(50.0, size_dollars) for size_dollars in (10.0, 20.0, 44.0, 87.5, 100.0)]
    assert fees == sorted(fees)


def test_fee_non_increasing_as_price_rises_for_fixed_dollars(risk):
    # For a fixed dollar stake the fee scales with (1 - price), so it falls as
    # the contract price rises toward $1.
    fees = [risk.estimate_fee_dollars(price_cents, 100.0) for price_cents in (10.0, 25.0, 50.0, 75.0, 90.0)]
    assert fees == sorted(fees, reverse=True)


def test_fee_subcent_exposure_rounds_up_to_one_cent(risk):
    # A tiny but non-zero taker fee (~0.07¢ here) still rounds up to a full cent.
    fee = risk.estimate_fee_dollars(price_cents=99.0, size_dollars=1.0)
    assert fee == 0.01


@pytest.mark.parametrize(
    "price_cents,size_dollars",
    [(-5.0, 100.0), (0.0, 100.0), (100.0, 100.0), (150.0, 100.0), (50.0, 0.0), (50.0, -10.0)],
)
def test_fee_zero_for_out_of_range_inputs(risk, price_cents, size_dollars):
    assert risk.estimate_fee_dollars(price_cents, size_dollars) == 0.0


def test_check_trade_fee_estimate_uses_corrected_fee(risk, sample_idea, empty_portfolio):
    decision = risk.check_trade(sample_idea, empty_portfolio)
    assert decision.approved
    assert decision.fees_estimate_cents > 0
    # The decision's fee must come from estimate_fee_dollars at the trade's price
    # and size (within a cent, since check_trade sizes before rounding to cents).
    recomputed_fee_cents = (
        risk.estimate_fee_dollars(sample_idea.market_price, decision.approved_size_dollars) * 100.0
    )
    assert decision.fees_estimate_cents == pytest.approx(recomputed_fee_cents, abs=1.0)


def test_adaptive_sizing_reduces_after_three_losses(risk, empty_portfolio):
    # A modest-edge idea whose quarter-Kelly size lands below MAX_SINGLE_POSITION
    # yet above MIN_SINGLE_POSITION even after the post-loss halving, so the
    # reduction is observable rather than masked by the cap or floored out.
    modest_idea = TradeIdea(
        agent_id="conditional_event",
        ticker="NBA-CELTICS-WIN",
        side=Side.YES,
        action=OrderAction.BUY,
        confidence=0.30,
        market_price=20.0,
        reasoning="Slight edge",
        signal_sources=["A1"],
        category="sports",
    )
    risk.record_loss("sports")
    risk.record_loss("sports")
    risk.record_loss("sports")
    reduced = risk.check_trade(modest_idea, empty_portfolio)

    risk2 = RiskManager()
    full = risk2.check_trade(modest_idea, empty_portfolio)

    assert reduced.approved
    assert full.approved
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
