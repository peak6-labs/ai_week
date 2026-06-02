"""Tests for the dashboard's raw-JSON -> response-dict mapping.

Exercises the prod-shape gotchas: side derived from the sign of position_fp,
fixed-point dollar/contract strings, average-cost math, NO-side pricing, empty
state, and zero-quantity filtering.
"""
from datetime import datetime, timezone

from kalshi_trader.dashboard import portfolio_mapping
from kalshi_trader.models import Market


def _market(ticker, last_price, *, yes_bid=0.0, yes_ask=0.0, category="politics", title="A market?"):
    return Market(
        ticker=ticker, event_ticker=ticker.split("-")[0], series_ticker=ticker.split("-")[0],
        title=title, yes_bid=yes_bid, yes_ask=yes_ask, last_price=last_price,
        volume_24h=100, open_interest=500, category=category,
        close_time=datetime(2026, 6, 3, 21, 0, tzinfo=timezone.utc), status="open",
    )


def test_parse_fixed_point_handles_strings_and_blanks():
    assert portfolio_mapping.parse_fixed_point("10.00") == 10.0
    assert portfolio_mapping.parse_fixed_point("-3.50") == -3.5
    assert portfolio_mapping.parse_fixed_point("") == 0.0
    assert portfolio_mapping.parse_fixed_point(None) == 0.0


def test_balance_prefers_integer_cents():
    mapped = portfolio_mapping.map_balance(
        {"balance": 41255, "portfolio_value": 50310, "balance_dollars": "412.55", "updated_ts": 1_780_000_000}
    )
    assert mapped["balance_dollars"] == 412.55
    assert mapped["portfolio_value_dollars"] == 503.10


def test_yes_position_side_avg_and_unrealized():
    # 50 YES contracts, $20.50 cost basis => avg 41c. Current YES last = 47c.
    raw = {
        "ticker": "KXHIGHNY-26JUN02-B57.5", "position_fp": "50.00",
        "market_exposure_dollars": "20.50", "total_traded_dollars": "20.50",
        "realized_pnl_dollars": "1.20", "fees_paid_dollars": "0.15",
    }
    lookup = {"KXHIGHNY-26JUN02-B57.5": _market("KXHIGHNY-26JUN02-B57.5", 47.0, category="climate and weather")}
    mapped = portfolio_mapping.map_position(raw, lookup)
    assert mapped["side"] == "yes"
    assert mapped["quantity"] == 50.0
    assert mapped["avg_price_cents"] == 41.0
    assert mapped["current_price_cents"] == 47.0
    # (0.47 - 0.41) * 50 = 3.00
    assert mapped["unrealized_pnl_dollars"] == 3.00
    assert mapped["category"] == "climate and weather"
    assert mapped["kalshi_url"] == "https://kalshi.com/markets/kxhighny"


def test_no_position_uses_complement_price():
    # Negative position_fp => NO side. 20 NO @ avg 30c cost. Current YES last = 60c
    # => current NO price = 40c. Unrealized = (0.40 - 0.30) * 20 = +2.00
    raw = {
        "ticker": "KXFOO-X", "position_fp": "-20.00",
        "market_exposure_dollars": "6.00", "total_traded_dollars": "6.00",
        "realized_pnl_dollars": "0", "fees_paid_dollars": "0",
    }
    lookup = {"KXFOO-X": _market("KXFOO-X", 60.0)}
    mapped = portfolio_mapping.map_position(raw, lookup)
    assert mapped["side"] == "no"
    assert mapped["quantity"] == 20.0
    assert mapped["avg_price_cents"] == 30.0
    assert mapped["current_price_cents"] == 40.0
    assert mapped["unrealized_pnl_dollars"] == 2.00


def test_position_without_market_data_emits_nulls():
    raw = {"ticker": "KXNOJOIN-Y", "position_fp": "5.00", "market_exposure_dollars": "2.50"}
    mapped = portfolio_mapping.map_position(raw, {})  # no market in lookup
    assert mapped["current_price_cents"] is None
    assert mapped["unrealized_pnl_dollars"] is None
    assert mapped["category"] == "unknown"
    assert mapped["avg_price_cents"] == 50.0  # cost basis still derivable


def test_summarize_excludes_closed_from_exposure_but_keeps_realized():
    held = {"ticker": "KXA-1", "position_fp": "10.00", "market_exposure_dollars": "5.00",
            "realized_pnl_dollars": "1.00", "total_traded_dollars": "5.00", "fees_paid_dollars": "0"}
    closed = {"ticker": "KXB-2", "position_fp": "0.00", "market_exposure_dollars": "0",
              "realized_pnl_dollars": "4.00", "total_traded_dollars": "9.00", "fees_paid_dollars": "0"}
    lookup = {"KXA-1": _market("KXA-1", 70.0, category="politics")}
    mapped = portfolio_mapping.map_positions([held, closed], lookup)

    assert len(portfolio_mapping.open_positions(mapped)) == 1  # closed filtered out
    summary = portfolio_mapping.summarize_positions(mapped)
    assert summary["open_positions_count"] == 1
    assert summary["total_exposure_dollars"] == 5.00          # closed contributes 0
    assert summary["realized_pnl_dollars"] == 5.00            # 1.00 + 4.00 lifetime
    assert summary["exposure_by_category"] == {"politics": 5.00}
    assert summary["exposure_limit_dollars"] == portfolio_mapping.EXPOSURE_LIMIT_DOLLARS


def test_orders_pick_side_price_and_drop_fully_filled():
    yes_order = {"order_id": "a", "ticker": "KXYES-1", "outcome_side": "yes",
                 "type": "limit", "status": "resting", "yes_price_dollars": "0.47",
                 "no_price_dollars": "0.53", "remaining_count_fp": "12.00",
                 "initial_count_fp": "12.00", "fill_count_fp": "0.00", "created_time": "2026-06-02T17:00:00Z"}
    no_order = {"order_id": "b", "ticker": "KXNO-2", "outcome_side": "no",
                "type": "limit", "status": "resting", "yes_price_dollars": "0.69",
                "no_price_dollars": "0.31", "remaining_count_fp": "8.00",
                "initial_count_fp": "8.00", "fill_count_fp": "0.00", "created_time": None}
    filled = {"order_id": "c", "ticker": "KXDONE-3", "outcome_side": "yes",
              "yes_price_dollars": "0.50", "no_price_dollars": "0.50",
              "remaining_count_fp": "0.00", "initial_count_fp": "5.00", "fill_count_fp": "5.00"}
    mapped = portfolio_mapping.map_orders([yes_order, no_order, filled], {})
    assert len(mapped) == 2                       # fully-filled dropped
    assert mapped[0]["price_cents"] == 47.0       # yes side -> yes_price
    assert mapped[1]["price_cents"] == 31.0       # no side  -> no_price
    assert mapped[0]["kalshi_url"] == "https://kalshi.com/markets/kxyes"


def test_empty_state_is_clean():
    assert portfolio_mapping.map_positions([], {}) == []
    assert portfolio_mapping.map_orders([], {}) == []
    summary = portfolio_mapping.summarize_positions([])
    assert summary["open_positions_count"] == 0
    assert summary["total_exposure_dollars"] == 0
    assert summary["exposure_by_category"] == {}
