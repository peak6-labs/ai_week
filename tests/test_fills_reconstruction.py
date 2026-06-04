"""Tests for the fills-based closed position reconstruction logic."""
import pytest
from kalshi_trader.ui.server import compute_closed_positions


def _fill(ticker, side, action, count, yes_price, created_time="2026-06-01T10:00:00Z", fee_cost=0.0):
    return {
        "ticker": ticker,
        "side": side,
        "action": action,
        "count": count,
        "yes_price": yes_price,
        "fee_cost": fee_cost,
        "created_time": created_time,
    }


# ---------------------------------------------------------------------------
# Basic open/closed detection
# ---------------------------------------------------------------------------

def test_empty_cache_returns_empty():
    assert compute_closed_positions({}, set()) == []


def test_ticker_with_only_buys_is_not_closed():
    cache = {"FOO-1": [_fill("FOO-1", "yes", "buy", 5, 30)]}
    assert compute_closed_positions(cache, set()) == []


def test_ticker_partially_sold_is_not_closed():
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 10, 30),
            _fill("FOO-1", "yes", "sell", 5, 50),
        ]
    }
    assert compute_closed_positions(cache, set()) == []


def test_fully_sold_yes_position_is_closed():
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 5, 30),
            _fill("FOO-1", "yes", "sell", 5, 50),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert len(result) == 1
    assert result[0]["ticker"] == "FOO-1"


def test_ticker_in_open_positions_is_excluded():
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 5, 30),
            _fill("FOO-1", "yes", "sell", 5, 50),
        ]
    }
    result = compute_closed_positions(cache, {"FOO-1"})
    assert result == []


# ---------------------------------------------------------------------------
# YES position field values
# ---------------------------------------------------------------------------

def test_yes_position_side_is_uppercase_yes():
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 5, 30),
            _fill("FOO-1", "yes", "sell", 5, 50),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["side"] == "YES"


def test_yes_position_entry_price_is_avco_of_buy_yes_prices():
    # Two buy fills: 4 contracts at 20¢ + 6 contracts at 30¢ → AVCO = (80+180)/10 = 26¢
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 4, 20),
            _fill("FOO-1", "yes", "buy", 6, 30),
            _fill("FOO-1", "yes", "sell", 10, 60),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["entry_price_cents"] == pytest.approx(26.0)


def test_yes_position_exit_price_is_avco_of_sell_yes_prices():
    # Two sell fills: 3 contracts at 70¢ + 7 contracts at 90¢ → AVCO = (210+630)/10 = 84¢
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 10, 30),
            _fill("FOO-1", "yes", "sell", 3, 70),
            _fill("FOO-1", "yes", "sell", 7, 90),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["exit_price_cents"] == pytest.approx(84.0)


def test_yes_position_realized_pnl():
    # 10 contracts, entry 30¢, exit 60¢ → PnL = (60-30) * 10 / 100 = $3.00
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 10, 30),
            _fill("FOO-1", "yes", "sell", 10, 60),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["realized_pnl_dollars"] == pytest.approx(3.00)


def test_yes_position_loss():
    # 10 contracts, entry 50¢, exit 20¢ → PnL = (20-50) * 10 / 100 = -$3.00
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 10, 50),
            _fill("FOO-1", "yes", "sell", 10, 20),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["realized_pnl_dollars"] == pytest.approx(-3.00)


def test_yes_position_contracts():
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 7, 30),
            _fill("FOO-1", "yes", "sell", 7, 60),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["contracts"] == 7


# ---------------------------------------------------------------------------
# NO position field values
# ---------------------------------------------------------------------------

def test_no_position_side_is_uppercase_no():
    cache = {
        "BAR-1": [
            _fill("BAR-1", "no", "buy", 5, 70),   # no_price = 30¢
            _fill("BAR-1", "no", "sell", 5, 50),  # no_price = 50¢
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["side"] == "NO"


def test_no_position_entry_price_is_no_price():
    # Buy NO at yes_price=70 → no_price entry = 100-70 = 30¢
    cache = {
        "BAR-1": [
            _fill("BAR-1", "no", "buy", 5, 70),
            _fill("BAR-1", "no", "sell", 5, 50),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["entry_price_cents"] == pytest.approx(30.0)


def test_no_position_exit_price_is_no_price():
    # Sell NO at yes_price=50 → no_price exit = 100-50 = 50¢
    cache = {
        "BAR-1": [
            _fill("BAR-1", "no", "buy", 5, 70),
            _fill("BAR-1", "no", "sell", 5, 50),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["exit_price_cents"] == pytest.approx(50.0)


def test_no_position_realized_pnl():
    # 5 contracts NO, entry no_price=30¢, exit no_price=50¢ → PnL = (50-30)*5/100 = $1.00
    cache = {
        "BAR-1": [
            _fill("BAR-1", "no", "buy", 5, 70),   # no_price = 30¢
            _fill("BAR-1", "no", "sell", 5, 50),  # no_price = 50¢
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["realized_pnl_dollars"] == pytest.approx(1.00)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def test_opened_at_is_earliest_buy_fill():
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 3, 30, "2026-06-01T10:00:00Z"),
            _fill("FOO-1", "yes", "buy", 7, 30, "2026-06-02T10:00:00Z"),
            _fill("FOO-1", "yes", "sell", 10, 60, "2026-06-03T10:00:00Z"),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["opened_at"] == "2026-06-01T10:00:00Z"


def test_closed_at_is_latest_sell_fill():
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 10, 30, "2026-06-01T10:00:00Z"),
            _fill("FOO-1", "yes", "sell", 4, 60, "2026-06-03T08:00:00Z"),
            _fill("FOO-1", "yes", "sell", 6, 65, "2026-06-03T10:00:00Z"),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["closed_at"] == "2026-06-03T10:00:00Z"


# ---------------------------------------------------------------------------
# Multiple tickers
# ---------------------------------------------------------------------------

def test_multiple_tickers_some_open_some_closed():
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 5, 30),
            _fill("FOO-1", "yes", "sell", 5, 60),
        ],
        "BAR-1": [
            _fill("BAR-1", "yes", "buy", 3, 20),
            # not fully sold — still open
        ],
        "BAZ-1": [
            _fill("BAZ-1", "no", "buy", 10, 80),
            _fill("BAZ-1", "no", "sell", 10, 60),
        ],
    }
    result = compute_closed_positions(cache, set())
    tickers = {r["ticker"] for r in result}
    assert tickers == {"FOO-1", "BAZ-1"}
    assert "BAR-1" not in tickers


def test_fees_deducted_from_realized_pnl():
    # 10 contracts YES, entry 30¢, exit 60¢ → gross = $3.00, fees = $0.10 → net = $2.90
    cache = {
        "FOO-1": [
            _fill("FOO-1", "yes", "buy", 6, 30, fee_cost=0.06),
            _fill("FOO-1", "yes", "buy", 4, 30, fee_cost=0.04),
            _fill("FOO-1", "yes", "sell", 10, 60),
        ]
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["realized_pnl_dollars"] == pytest.approx(2.90)


def test_output_sorted_newest_closed_first():
    cache = {
        "OLD-1": [
            _fill("OLD-1", "yes", "buy", 5, 30, "2026-05-01T10:00:00Z"),
            _fill("OLD-1", "yes", "sell", 5, 60, "2026-05-10T10:00:00Z"),
        ],
        "NEW-1": [
            _fill("NEW-1", "yes", "buy", 5, 30, "2026-06-01T10:00:00Z"),
            _fill("NEW-1", "yes", "sell", 5, 60, "2026-06-03T10:00:00Z"),
        ],
    }
    result = compute_closed_positions(cache, set())
    assert result[0]["ticker"] == "NEW-1"
    assert result[1]["ticker"] == "OLD-1"
