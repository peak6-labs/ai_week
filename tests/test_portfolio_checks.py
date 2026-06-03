"""Tests for the deterministic portfolio exit-check functions.

Position dicts passed to checks contain:
  market_exposure_dollars  — cost basis in dollars
  quantity                 — number of contracts (abs value)
  current_price_cents      — side-relative midpoint price in cents
  midpoint_yes_price_cents — always in YES terms (for the limit order)
"""
import pytest
from kalshi_trader.portfolio_checks import check_stop_loss, check_profit_target, EXIT_CHECKS


def _position(*, cost_basis: float, quantity: float, current_price_cents: float,
               midpoint_yes_price_cents: float | None = None) -> dict:
    return {
        "ticker": "KXTEST-1",
        "side": "yes",
        "quantity": quantity,
        "market_exposure_dollars": cost_basis,
        "current_price_cents": current_price_cents,
        "midpoint_yes_price_cents": midpoint_yes_price_cents if midpoint_yes_price_cents is not None else current_price_cents,
    }


# ---------------------------------------------------------------------------
# check_stop_loss
# ---------------------------------------------------------------------------

def test_stop_loss_triggers_when_down_30_percent():
    # cost_basis=$5, quantity=10, current=35c → value=$3.50 < 0.75×$5=$3.75
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=35.0)
    signal = check_stop_loss(position)
    assert signal is not None
    assert signal.reason == "stop_loss"
    assert "30%" in signal.description
    assert signal.exit_price_cents == 35.0


def test_stop_loss_does_not_trigger_when_down_20_percent():
    # cost_basis=$5, quantity=10, current=40c → value=$4.00 > 0.75×$5=$3.75
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=40.0)
    assert check_stop_loss(position) is None


def test_stop_loss_does_not_trigger_at_exact_threshold():
    # cost_basis=$5, quantity=10, current=37.5c → value=$3.75 = 0.75×$5 (not strictly less)
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=37.5)
    assert check_stop_loss(position) is None


def test_stop_loss_skips_zero_cost_basis():
    position = _position(cost_basis=0.0, quantity=10.0, current_price_cents=35.0)
    assert check_stop_loss(position) is None


def test_stop_loss_skips_zero_quantity():
    position = _position(cost_basis=5.0, quantity=0.0, current_price_cents=35.0)
    assert check_stop_loss(position) is None


# ---------------------------------------------------------------------------
# check_profit_target
# ---------------------------------------------------------------------------

def test_profit_target_triggers_when_up_80_percent():
    # cost_basis=$5, quantity=10, current=90c → value=$9.00 > 1.75×$5=$8.75
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=90.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"
    assert "80%" in signal.description
    assert signal.exit_price_cents == 90.0


def test_profit_target_does_not_trigger_when_up_60_percent():
    # cost_basis=$5, quantity=10, current=80c → value=$8.00 < 1.75×$5=$8.75
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=80.0)
    assert check_profit_target(position) is None


def test_profit_target_does_not_trigger_at_exact_threshold():
    # cost_basis=$5, quantity=10, current=87.5c → value=$8.75 = 1.75×$5 (not strictly greater)
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=87.5)
    assert check_profit_target(position) is None


def test_profit_target_skips_zero_cost_basis():
    position = _position(cost_basis=0.0, quantity=10.0, current_price_cents=90.0)
    assert check_profit_target(position) is None


def test_profit_target_skips_zero_quantity():
    position = _position(cost_basis=5.0, quantity=0.0, current_price_cents=90.0)
    assert check_profit_target(position) is None


# ---------------------------------------------------------------------------
# EXIT_CHECKS list
# ---------------------------------------------------------------------------

def test_exit_checks_contains_stop_loss_and_profit_target():
    assert check_stop_loss in EXIT_CHECKS
    assert check_profit_target in EXIT_CHECKS


def test_exit_checks_stop_loss_comes_before_profit_target():
    assert EXIT_CHECKS.index(check_stop_loss) < EXIT_CHECKS.index(check_profit_target)
