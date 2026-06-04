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
    PROFIT_TARGET_MULTIPLE,
    PROFIT_CONVERGENCE_FRACTION,
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
# check_profit_target
# ---------------------------------------------------------------------------

def test_profit_target_triggers_when_up_80_percent():
    # 50¢ entry (cost_basis=$5, quantity=10). Target = 50 + 0.75*(100-50) = 87.5¢.
    # At 90¢ → triggers.
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=90.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"
    assert "80%" in signal.description
    assert signal.exit_price_cents == 90.0


def test_profit_target_does_not_trigger_when_up_48_percent():
    # 50¢ entry. Target = min(75, 87.5) = 75¢. At 74¢ → no trigger.
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=74.0)
    assert check_profit_target(position) is None


def test_profit_target_does_not_trigger_at_exact_threshold():
    # 50¢ entry. Target = 75¢ exactly (not strictly greater) → no trigger.
    position = _position(cost_basis=5.0, quantity=10.0, current_price_cents=75.0)
    assert check_profit_target(position) is None


def test_profit_target_low_entry_exits_at_double():
    # 15¢ entry: target = min(30, 78.75) = 30¢. At 31¢ → triggers.
    position = _position(cost_basis=15.0, quantity=100.0, current_price_cents=31.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"


def test_profit_target_low_entry_holds_below_1_5x():
    # 15¢ entry: target = min(22.5, 78.75) = 22.5¢. At 22¢ → no trigger.
    position = _position(cost_basis=15.0, quantity=100.0, current_price_cents=22.0)
    assert check_profit_target(position) is None


def test_profit_target_9c_entry_exits_at_double():
    # 9¢ entry: target = min(18, 77.25) = 18¢. At 19¢ → triggers.
    position = _position(cost_basis=9.0, quantity=100.0, current_price_cents=19.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"


def test_profit_target_high_entry_triggers_via_convergence():
    # 75¢ entry: target = min(150, 93.75) = 93.75¢. Old 1.75× rule (131¢) was impossible.
    # At 94¢ → triggers.
    position = _position(cost_basis=7.5, quantity=10.0, current_price_cents=94.0)
    signal = check_profit_target(position)
    assert signal is not None
    assert signal.reason == "profit_target"


def test_profit_target_threshold_consistent():
    # Verify the exact trigger boundary for representative entry prices.
    # Below ~60¢: 1.5x rule wins. Above ~60¢: convergence rule wins.
    cases = [
        (9.0,  13.5),   # min(13.5,  77.25) = 13.5  — 1.5x
        (15.0, 22.5),   # min(22.5,  78.75) = 22.5  — 1.5x
        (25.0, 37.5),   # min(37.5,  81.25) = 37.5  — 1.5x
        (50.0, 75.0),   # min(75,    87.5 ) = 75.0  — 1.5x
        (75.0, 93.75),  # min(112.5, 93.75) = 93.75 — convergence
    ]
    for entry_cents, target_cents in cases:
        quantity = 100.0
        cost_basis = entry_cents * quantity / 100.0
        pos_at = _position(cost_basis=cost_basis, quantity=quantity, current_price_cents=target_cents)
        assert check_profit_target(pos_at) is None, f"should not trigger at threshold for entry={entry_cents}¢"
        pos_above = _position(cost_basis=cost_basis, quantity=quantity, current_price_cents=target_cents + 0.01)
        assert check_profit_target(pos_above) is not None, f"should trigger just above threshold for entry={entry_cents}¢"


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
