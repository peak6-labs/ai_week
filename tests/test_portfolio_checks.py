"""Tests for the deterministic portfolio exit-check functions.

Position dicts passed to checks contain:
  market_exposure_dollars  — cost basis in dollars
  quantity                 — number of contracts (abs value)
  current_price_cents      — side-relative midpoint price in cents
  midpoint_yes_price_cents — always in YES terms (for the limit order)
"""
import pytest
from kalshi_trader.portfolio_checks import (
    check_stop_loss,
    check_profit_target,
    EXIT_CHECKS,
    _profit_target_multiple,
)


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
# _profit_target_multiple — scaling by entry price
# ---------------------------------------------------------------------------

def test_profit_target_multiple_longshot():
    assert _profit_target_multiple(10.0) == 1.75

def test_profit_target_multiple_below_15():
    assert _profit_target_multiple(14.9) == 1.75

def test_profit_target_multiple_15_to_30():
    assert _profit_target_multiple(15.0) == 1.50
    assert _profit_target_multiple(29.9) == 1.50

def test_profit_target_multiple_30_to_50():
    assert _profit_target_multiple(30.0) == 1.35
    assert _profit_target_multiple(49.9) == 1.35

def test_profit_target_multiple_high_entry():
    assert _profit_target_multiple(50.0) == 1.25
    assert _profit_target_multiple(75.0) == 1.25


# ---------------------------------------------------------------------------
# check_profit_target
# ---------------------------------------------------------------------------

def test_profit_target_high_entry_triggers_at_25_percent():
    # 50¢ entry (cost=$5, qty=10). Threshold=1.25×. Target value=$6.25 → price=62.5¢.
    # At 63¢ → triggers.
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=63.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"


def test_profit_target_high_entry_does_not_trigger_below_25_percent():
    # 50¢ entry. At 62¢ (just below 62.5¢ threshold) → no trigger.
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=62.0)
    assert check_profit_target(position) is None


def test_profit_target_high_entry_does_not_trigger_at_exact_threshold():
    # 50¢ entry. Exactly at 62.5¢ (value=$6.25 = 1.25×$5, not strictly greater) → no trigger.
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=62.5)
    assert check_profit_target(position) is None


def test_profit_target_mid_entry_triggers_at_35_percent():
    # 40¢ entry (cost=$4, qty=10). Threshold=1.35×. Target value=$5.40 → price=54¢.
    # At 55¢ → triggers.
    position = _position(cost_basis=4.0, quantity=10.0, current_price_cents=55.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"


def test_profit_target_mid_entry_does_not_trigger_below_threshold():
    # 40¢ entry. At 53¢ (below 54¢ threshold) → no trigger.
    position = _position(cost_basis=4.0, quantity=10.0, current_price_cents=53.0)
    assert check_profit_target(position) is None


def test_profit_target_low_entry_triggers_at_50_percent():
    # 20¢ entry (cost=$20, qty=100). Threshold=1.50×. Target value=$30 → price=30¢.
    # At 31¢ → triggers.
    position = _position(cost_basis=20.0, quantity=100.0, current_price_cents=31.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"


def test_profit_target_low_entry_holds_below_50_percent():
    # 20¢ entry. At 29¢ → no trigger.
    position = _position(cost_basis=20.0, quantity=100.0, current_price_cents=29.0)
    assert check_profit_target(position) is None


def test_profit_target_longshot_triggers_at_75_percent():
    # 9¢ entry (cost=$9, qty=100). Threshold=1.75×. Target value=$15.75 → price=15.75¢.
    # At 16¢ → triggers.
    position = _position(cost_basis=9.0, quantity=100.0, current_price_cents=16.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"


def test_profit_target_longshot_holds_below_75_percent():
    # 9¢ entry. At 15¢ (below 15.75¢ threshold) → no trigger.
    position = _position(cost_basis=9.0, quantity=100.0, current_price_cents=15.0)
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
