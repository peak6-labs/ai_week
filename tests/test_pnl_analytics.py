"""Tests for the pure P&L analytics over closed + open positions.

Covers the open/closed/early-exit/partial lifecycle, the summary metrics
(quality metrics computed on closed trades only), the cumulative time series,
the days-to-settlement bucketing, the groupings, and the net/gross basis.
"""
from datetime import datetime, timezone

import pytest

from kalshi_trader.ui.pnl_analytics import (
    bucket_days_to_settlement,
    build_analytics,
    compute_pnl_over_time,
    compute_summary_metrics,
    group_by_days_to_settlement,
    group_by_market_type,
    normalize_positions,
)

NOW = datetime(2026, 6, 5, 0, 0, 0, tzinfo=timezone.utc)


def _closed(
    ticker,
    *,
    side="YES",
    contracts=10,
    entry_price_cents=30.0,
    exit_price_cents=60.0,
    opened_at="2026-06-01T00:00:00Z",
    closed_at="2026-06-03T00:00:00Z",
    realized_pnl_dollars=3.0,
    gross_realized_pnl_dollars=None,
):
    return {
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": entry_price_cents,
        "exit_price_cents": exit_price_cents,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "realized_pnl_dollars": realized_pnl_dollars,
        "gross_realized_pnl_dollars": (
            gross_realized_pnl_dollars if gross_realized_pnl_dollars is not None
            else realized_pnl_dollars
        ),
    }


def _open(
    ticker,
    *,
    side="YES",
    quantity=10,
    avg_price_dollars=0.40,
    current_price_dollars=0.55,
    unrealized_pnl_dollars=1.5,
    gross_unrealized_pnl_dollars=None,
    realized_pnl_dollars=0.0,
):
    return {
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "avg_price_dollars": avg_price_dollars,
        "current_price_dollars": current_price_dollars,
        "unrealized_pnl_dollars": unrealized_pnl_dollars,
        "gross_unrealized_pnl_dollars": (
            gross_unrealized_pnl_dollars if gross_unrealized_pnl_dollars is not None
            else unrealized_pnl_dollars
        ),
        "realized_pnl_dollars": realized_pnl_dollars,
    }


def _rows(closed=None, open_=None, metadata=None, opened_at=None, basis="net"):
    return normalize_positions(
        closed or [], open_ or [], metadata or {}, opened_at or {}, NOW, basis=basis
    )


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

def test_empty_inputs_produce_zero_summary():
    payload = build_analytics([], [], {}, {}, NOW)
    summary = payload["summary"]
    assert summary["closed_trade_count"] == 0
    assert summary["open_trade_count"] == 0
    assert summary["realized_pnl_dollars"] == 0.0
    assert summary["unrealized_pnl_dollars"] == 0.0
    assert summary["total_pnl_dollars"] == 0.0
    assert summary["win_rate"] is None
    assert summary["profit_factor"] is None
    assert summary["max_drawdown_dollars"] == 0.0
    assert payload["pnl_over_time"] == []
    assert payload["current_total_pnl_dollars"] == 0.0
    assert payload["trades"] == []


# ---------------------------------------------------------------------------
# Lifecycle / normalization
# ---------------------------------------------------------------------------

def test_closed_held_to_settlement_row():
    rows = _rows(closed=[_closed("FOO-1", exit_price_cents=100.0, realized_pnl_dollars=7.0)])
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "closed"
    assert row["is_mark"] is False
    assert row["exit_or_mark_price_cents"] == 100.0
    assert row["realized_pnl_dollars"] == 7.0
    assert row["unrealized_pnl_dollars"] == 0.0
    assert row["pnl_dollars"] == 7.0


def test_closed_early_exit_keeps_real_exit_price():
    # Sold out before settlement at 71c — must NOT be coerced to 0/100.
    rows = _rows(closed=[_closed("FOO-1", exit_price_cents=71.0, realized_pnl_dollars=4.1)])
    assert rows[0]["exit_or_mark_price_cents"] == 71.0
    assert rows[0]["is_mark"] is False


def test_open_untouched_only_unrealized():
    rows = _rows(open_=[_open("BAR-1", unrealized_pnl_dollars=5.0, realized_pnl_dollars=0.0)])
    row = rows[0]
    assert row["status"] == "open"
    assert row["is_mark"] is True
    assert row["realized_pnl_dollars"] == 0.0
    assert row["unrealized_pnl_dollars"] == 5.0
    assert row["pnl_dollars"] == 5.0


def test_open_partial_exit_has_locked_in_and_unrealized():
    rows = _rows(open_=[_open("BAR-1", realized_pnl_dollars=3.0, unrealized_pnl_dollars=5.0)])
    row = rows[0]
    assert row["realized_pnl_dollars"] == 3.0      # locked-in from the partial sell-out
    assert row["unrealized_pnl_dollars"] == 5.0     # remaining contracts, marked
    assert row["pnl_dollars"] == 5.0                # sort key is unrealized for open rows


def test_open_holding_days_uses_now():
    rows = _rows(
        open_=[_open("BAR-1")],
        opened_at={"BAR-1": "2026-06-03T00:00:00Z"},
    )
    # now (06-05) - opened (06-03) == 2 days
    assert rows[0]["holding_days"] == pytest.approx(2.0)


def test_open_entry_price_from_avg_price_dollars():
    rows = _rows(open_=[_open("BAR-1", avg_price_dollars=0.41, current_price_dollars=0.55)])
    assert rows[0]["entry_price_cents"] == pytest.approx(41.0)
    assert rows[0]["exit_or_mark_price_cents"] == pytest.approx(55.0)


def test_closed_holding_days():
    rows = _rows(closed=[_closed("FOO-1", opened_at="2026-06-01T00:00:00Z", closed_at="2026-06-03T00:00:00Z")])
    assert rows[0]["holding_days"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------

def test_realized_includes_open_locked_in():
    rows = _rows(
        closed=[_closed("A", realized_pnl_dollars=10.0)],
        open_=[_open("B", realized_pnl_dollars=3.0, unrealized_pnl_dollars=5.0)],
    )
    summary = compute_summary_metrics(rows)
    assert summary["realized_pnl_dollars"] == 13.0   # 10 closed + 3 open locked-in
    assert summary["unrealized_pnl_dollars"] == 5.0
    assert summary["total_pnl_dollars"] == 18.0


def test_quality_metrics_use_closed_rows_only():
    rows = _rows(
        closed=[
            _closed("WIN", realized_pnl_dollars=10.0),
            _closed("LOSE", realized_pnl_dollars=-4.0),
        ],
        # An open winner must not affect win rate / profit factor / expectancy.
        open_=[_open("OPEN", realized_pnl_dollars=0.0, unrealized_pnl_dollars=7.0)],
    )
    summary = compute_summary_metrics(rows)
    assert summary["closed_trade_count"] == 2
    assert summary["open_trade_count"] == 1
    assert summary["win_rate"] == pytest.approx(0.5)
    assert summary["winning_trade_count"] == 1
    assert summary["losing_trade_count"] == 1
    assert summary["average_win_dollars"] == pytest.approx(10.0)
    assert summary["average_loss_dollars"] == pytest.approx(4.0)
    assert summary["payoff_ratio"] == pytest.approx(2.5)
    assert summary["profit_factor"] == pytest.approx(2.5)
    assert summary["expectancy_dollars"] == pytest.approx(3.0)  # (10 - 4) / 2


def test_profit_factor_none_when_no_losses():
    rows = _rows(closed=[_closed("A", realized_pnl_dollars=5.0), _closed("B", realized_pnl_dollars=2.0)])
    summary = compute_summary_metrics(rows)
    assert summary["profit_factor"] is None
    assert summary["payoff_ratio"] is None  # no losses → no average loss


def test_realized_return_on_capital():
    # entry 50c x 10 contracts = $5.00 deployed; realized $3.00 → ROI 0.6
    rows = _rows(closed=[_closed("A", entry_price_cents=50.0, contracts=10, realized_pnl_dollars=3.0)])
    summary = compute_summary_metrics(rows)
    assert summary["capital_deployed_dollars"] == pytest.approx(5.0)
    assert summary["realized_return_on_capital"] == pytest.approx(0.6)


def test_max_drawdown_on_closed_curve():
    rows = _rows(closed=[
        _closed("T1", realized_pnl_dollars=10.0, closed_at="2026-06-01T00:00:00Z"),
        _closed("T2", realized_pnl_dollars=-6.0, closed_at="2026-06-02T00:00:00Z"),
        _closed("T3", realized_pnl_dollars=2.0, closed_at="2026-06-03T00:00:00Z"),
    ])
    summary = compute_summary_metrics(rows)
    # cumulative 10, 4, 6 ; peak 10 ; drawdown 0, -6, -4 → max drawdown -6
    assert summary["max_drawdown_dollars"] == pytest.approx(-6.0)


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------

def test_pnl_over_time_sorted_and_cumulative():
    rows = _rows(closed=[
        _closed("LATE", realized_pnl_dollars=5.0, closed_at="2026-06-03T00:00:00Z"),
        _closed("EARLY", realized_pnl_dollars=2.0, closed_at="2026-06-01T00:00:00Z"),
    ])
    series, _ = compute_pnl_over_time(rows)
    assert [point["timestamp"] for point in series] == [
        "2026-06-01T00:00:00Z", "2026-06-03T00:00:00Z",
    ]
    assert [point["cumulative_pnl_dollars"] for point in series] == [2.0, 7.0]


def test_current_total_includes_open_unrealized_and_locked_in():
    rows = _rows(
        closed=[_closed("A", realized_pnl_dollars=10.0)],
        open_=[_open("B", realized_pnl_dollars=3.0, unrealized_pnl_dollars=5.0)],
    )
    _, current_total = compute_pnl_over_time(rows)
    assert current_total == pytest.approx(18.0)  # 10 + 3 + 5


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("days,expected", [
    (None, "unknown"),
    (0, "<1d"),
    (1, "1–3d"),
    (2, "1–3d"),
    (3, "3–7d"),
    (6, "3–7d"),
    (7, "1–2wk"),
    (13, "1–2wk"),
    (14, "2wk+"),
    (40, "2wk+"),
])
def test_bucket_boundaries(days, expected):
    assert bucket_days_to_settlement(days) == expected


def test_days_to_settlement_unknown_without_metadata():
    rows = _rows(closed=[_closed("FOO-1")])  # no metadata_lookup entry
    assert rows[0]["days_to_settlement"] is None


# ---------------------------------------------------------------------------
# Groupings
# ---------------------------------------------------------------------------

def test_group_by_market_type_mixed_open_closed_ordered():
    metadata = {
        "WX-1": {"market_type": "weather", "close_time": "2026-06-03T00:00:00Z"},
        "WX-2": {"market_type": "weather", "close_time": "2026-06-03T00:00:00Z"},
        "POL-1": {"market_type": "politics", "close_time": "2026-06-03T00:00:00Z"},
    }
    rows = _rows(
        closed=[
            _closed("WX-1", realized_pnl_dollars=8.0),
            _closed("POL-1", realized_pnl_dollars=2.0),
        ],
        open_=[_open("WX-2", realized_pnl_dollars=0.0, unrealized_pnl_dollars=3.0)],
        metadata=metadata,
        opened_at={"WX-2": "2026-06-02T00:00:00Z"},
    )
    grouped = group_by_market_type(rows)
    weather = next(group for group in grouped if group["market_type"] == "weather")
    politics = next(group for group in grouped if group["market_type"] == "politics")
    assert weather["closed_count"] == 1
    assert weather["open_count"] == 1
    assert weather["realized_pnl_dollars"] == 8.0
    assert weather["unrealized_pnl_dollars"] == 3.0
    assert weather["total_pnl_dollars"] == 11.0
    assert politics["total_pnl_dollars"] == 2.0
    # ordered by total P&L descending
    assert grouped[0]["market_type"] == "weather"


def test_group_by_days_to_settlement_fixed_order():
    metadata = {
        "NEAR": {"market_type": "x", "close_time": "2026-06-01T12:00:00Z"},   # ~0.5d → <1d
        "MID": {"market_type": "x", "close_time": "2026-06-05T00:00:00Z"},    # 4d → 3–7d
    }
    rows = _rows(
        closed=[
            _closed("NEAR", opened_at="2026-06-01T00:00:00Z", closed_at="2026-06-01T06:00:00Z"),
            _closed("MID", opened_at="2026-06-01T00:00:00Z", closed_at="2026-06-04T00:00:00Z"),
        ],
        metadata=metadata,
    )
    grouped = group_by_days_to_settlement(rows)
    labels = [group["bucket_label"] for group in grouped]
    # <1d must come before 3–7d (fixed bucket order, not insertion / total order)
    assert labels.index("<1d") < labels.index("3–7d")


# ---------------------------------------------------------------------------
# Basis (net vs gross)
# ---------------------------------------------------------------------------

def test_gross_basis_uses_gross_fields():
    rows = _rows(
        closed=[_closed("A", realized_pnl_dollars=9.2, gross_realized_pnl_dollars=10.0)],
        open_=[_open("B", unrealized_pnl_dollars=4.0, gross_unrealized_pnl_dollars=4.5)],
        basis="gross",
    )
    closed_row = next(row for row in rows if row["status"] == "closed")
    open_row = next(row for row in rows if row["status"] == "open")
    assert closed_row["realized_pnl_dollars"] == 10.0
    assert open_row["unrealized_pnl_dollars"] == 4.5


def test_net_basis_uses_net_fields():
    rows = _rows(
        closed=[_closed("A", realized_pnl_dollars=9.2, gross_realized_pnl_dollars=10.0)],
        open_=[_open("B", unrealized_pnl_dollars=4.0, gross_unrealized_pnl_dollars=4.5)],
        basis="net",
    )
    closed_row = next(row for row in rows if row["status"] == "closed")
    open_row = next(row for row in rows if row["status"] == "open")
    assert closed_row["realized_pnl_dollars"] == 9.2
    assert open_row["unrealized_pnl_dollars"] == 4.0


# ---------------------------------------------------------------------------
# Full payload shape
# ---------------------------------------------------------------------------

def test_build_analytics_payload_shape():
    metadata = {"WX-1": {"market_type": "weather", "close_time": "2026-06-03T00:00:00Z"}}
    payload = build_analytics(
        [_closed("WX-1", realized_pnl_dollars=5.0)],
        [_open("WX-2", realized_pnl_dollars=0.0, unrealized_pnl_dollars=2.0)],
        metadata,
        {"WX-2": "2026-06-04T00:00:00Z"},
        NOW,
    )
    for key in (
        "generated_at", "basis", "summary", "pnl_over_time",
        "current_total_pnl_dollars", "by_market_type", "by_days_to_settlement", "trades",
    ):
        assert key in payload
    assert payload["basis"] == "net"
    assert len(payload["trades"]) == 2
    assert {row["status"] for row in payload["trades"]} == {"closed", "open"}
