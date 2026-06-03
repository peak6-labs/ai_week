"""Deterministic exit-check functions for open positions.

Each function receives a position dict and returns an ExitSignal if the
position should be exited, or None if it should be held.

Position dict fields expected by all checks:
  market_exposure_dollars  (float) — cost basis paid, in dollars
  quantity                 (float) — number of contracts held (abs value)
  current_price_cents      (float) — side-relative midpoint price in cents
  midpoint_yes_price_cents (float) — midpoint in YES-price terms (for limit order)

To add a new check: write a function matching this signature and append it to
EXIT_CHECKS. The runner in scripts/evaluate_portfolio.py iterates EXIT_CHECKS
and uses the first non-None result.
"""
from __future__ import annotations

from kalshi_trader.models import ExitSignal

STOP_LOSS_THRESHOLD = 0.75        # exit if current_value < 75% of cost basis

# How far the price must travel from entry toward certainty (100¢) before we
# take profit. At 50¢ entry this equals the old 1.75× rule (target = 87.5¢).
# At 9¢ entry it requires ~77¢ (not 16¢), letting longshots run much further.
PROFIT_CONVERGENCE_FRACTION = 0.75


def check_stop_loss(position: dict) -> ExitSignal | None:
    cost_basis = position.get("market_exposure_dollars", 0.0)
    quantity = position.get("quantity", 0.0)
    current_price_cents = position.get("current_price_cents")
    if cost_basis <= 0 or quantity <= 0 or current_price_cents is None:
        return None
    current_value = quantity * current_price_cents / 100.0
    if current_value < STOP_LOSS_THRESHOLD * cost_basis:
        loss_pct = round((1.0 - current_value / cost_basis) * 100.0)
        return ExitSignal(
            reason="stop_loss",
            exit_price_cents=position["midpoint_yes_price_cents"],
            description=f"down {loss_pct}% from cost basis",
        )
    return None


def check_profit_target(position: dict) -> ExitSignal | None:
    cost_basis = position.get("market_exposure_dollars", 0.0)
    quantity = position.get("quantity", 0.0)
    current_price_cents = position.get("current_price_cents")
    if cost_basis <= 0 or quantity <= 0 or current_price_cents is None:
        return None
    entry_price_cents = (cost_basis / quantity) * 100.0
    target_price_cents = entry_price_cents + PROFIT_CONVERGENCE_FRACTION * (100.0 - entry_price_cents)
    if current_price_cents > target_price_cents:
        gain_pct = round((current_price_cents / entry_price_cents - 1.0) * 100.0)
        return ExitSignal(
            reason="profit_target",
            exit_price_cents=position["midpoint_yes_price_cents"],
            description=f"up {gain_pct}% from cost basis",
        )
    return None


EXIT_CHECKS = [
    check_stop_loss,
    check_profit_target,
]
