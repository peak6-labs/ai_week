"""Pure P&L analytics over closed + open positions.

Everything here is a pure function — no I/O. The FastAPI endpoint passes in the
already-collected positions plus two lookups (market metadata and the per-ticker
opened-at timestamp) and the current time, so the whole module is trivially
unit-testable.

Lifecycle handling (see the plan): every position is normalized into a single
"trade row" carrying both a realized and an unrealized P&L figure plus a
``status`` of ``"closed"`` or ``"open"``:

- Held to settlement / fully sold before settlement  → a row from
  ``closed_positions`` (status ``closed``); its ``exit_price_cents`` is the real
  exit (sell AVCO or settlement), so early exits show their true exit price.
- Partially sold, remainder still open               → a row from ``positions``
  (status ``open``); the locked-in portion is the open row's
  ``realized_pnl_dollars`` and the remainder is its ``unrealized_pnl_dollars``.
- Open, untouched                                    → a row from ``positions``
  with zero realized and only an unrealized mark.

Quality metrics (win rate, payoff ratio, profit factor, expectancy, realized
ROI, max drawdown) are computed on ``status == "closed"`` rows only, because an
open mark is not a settled outcome.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Days-to-settlement buckets, in display order. Each entry is
# (label, lower_days_inclusive, upper_days_exclusive). ``upper_days`` of None
# means "no upper bound".
_DAYS_TO_SETTLEMENT_BUCKETS: list[tuple[str, int, int | None]] = [
    ("<1d", 0, 1),
    ("1–3d", 1, 3),
    ("3–7d", 3, 7),
    ("1–2wk", 7, 14),
    ("2wk+", 14, None),
]
_UNKNOWN_BUCKET_LABEL = "unknown"


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp into an aware UTC datetime, tolerantly."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _days_between(earlier: datetime | None, later: datetime | None) -> float | None:
    """Whole-and-fractional days from ``earlier`` to ``later`` (None if either missing)."""
    if earlier is None or later is None:
        return None
    return (later - earlier).total_seconds() / 86400.0


def capital_for(trade_row: dict) -> float:
    """Dollars at risk on entry — for a binary long this is the max loss."""
    entry_price_cents = trade_row.get("entry_price_cents") or 0.0
    contracts = trade_row.get("contracts") or 0
    return entry_price_cents / 100.0 * contracts


def bucket_days_to_settlement(days_to_settlement: float | None) -> str:
    """Map a days-to-settlement value onto a display bucket label."""
    if days_to_settlement is None:
        return _UNKNOWN_BUCKET_LABEL
    for bucket_label, lower_days, upper_days in _DAYS_TO_SETTLEMENT_BUCKETS:
        if days_to_settlement >= lower_days and (upper_days is None or days_to_settlement < upper_days):
            return bucket_label
    return _UNKNOWN_BUCKET_LABEL


def _normalize_closed_position(
    closed_position: dict,
    metadata_lookup: dict[str, dict],
    *,
    basis: str,
) -> dict:
    """Turn one closed-position dict into a unified trade row."""
    ticker = closed_position.get("ticker", "")
    market_metadata = metadata_lookup.get(ticker, {})
    opened_at = _parse_timestamp(closed_position.get("opened_at"))
    closed_at = _parse_timestamp(closed_position.get("closed_at"))
    close_time = _parse_timestamp(market_metadata.get("close_time"))

    if basis == "gross":
        realized_pnl_dollars = closed_position.get(
            "gross_realized_pnl_dollars",
            closed_position.get("realized_pnl_dollars", 0.0),
        )
    else:
        realized_pnl_dollars = closed_position.get("realized_pnl_dollars", 0.0)

    days_to_settlement = _days_between(opened_at, close_time)
    return {
        "ticker": ticker,
        "status": "closed",
        "market_type": market_metadata.get("market_type") or "unknown",
        "side": closed_position.get("side", ""),
        "contracts": int(closed_position.get("contracts", 0) or 0),
        "entry_price_cents": closed_position.get("entry_price_cents"),
        "exit_or_mark_price_cents": closed_position.get("exit_price_cents"),
        "is_mark": False,
        "opened_at": closed_position.get("opened_at"),
        "closed_at": closed_position.get("closed_at"),
        "days_to_settlement": int(days_to_settlement) if days_to_settlement is not None else None,
        "holding_days": _days_between(opened_at, closed_at),
        "realized_pnl_dollars": float(realized_pnl_dollars or 0.0),
        "unrealized_pnl_dollars": 0.0,
        "pnl_dollars": float(realized_pnl_dollars or 0.0),
    }


def _normalize_open_position(
    open_position: dict,
    metadata_lookup: dict[str, dict],
    opened_at_lookup: dict[str, str],
    now: datetime,
    *,
    basis: str,
) -> dict:
    """Turn one open-position dict into a unified trade row."""
    ticker = open_position.get("ticker", "")
    market_metadata = metadata_lookup.get(ticker, {})
    opened_at = _parse_timestamp(opened_at_lookup.get(ticker))
    close_time = _parse_timestamp(market_metadata.get("close_time"))

    average_price_dollars = open_position.get("avg_price_dollars")
    current_price_dollars = open_position.get("current_price_dollars")
    entry_price_cents = round(average_price_dollars * 100.0, 2) if average_price_dollars is not None else None
    mark_price_cents = round(current_price_dollars * 100.0, 2) if current_price_dollars is not None else None

    # Open rows carry Kalshi's locked-in realized (from any partial sell-out)
    # plus the mark-to-market unrealized on the remaining contracts.
    realized_pnl_dollars = float(open_position.get("realized_pnl_dollars", 0.0) or 0.0)
    if basis == "gross":
        unrealized_pnl_dollars = open_position.get(
            "gross_unrealized_pnl_dollars",
            open_position.get("unrealized_pnl_dollars", 0.0),
        )
    else:
        unrealized_pnl_dollars = open_position.get("unrealized_pnl_dollars", 0.0)
    unrealized_pnl_dollars = float(unrealized_pnl_dollars or 0.0)

    days_to_settlement = _days_between(opened_at, close_time)
    return {
        "ticker": ticker,
        "status": "open",
        "market_type": market_metadata.get("market_type") or "unknown",
        "side": open_position.get("side", ""),
        "contracts": int(open_position.get("quantity", 0) or 0),
        "entry_price_cents": entry_price_cents,
        "exit_or_mark_price_cents": mark_price_cents,
        "is_mark": True,
        "opened_at": opened_at_lookup.get(ticker),
        "closed_at": None,
        "days_to_settlement": int(days_to_settlement) if days_to_settlement is not None else None,
        "holding_days": _days_between(opened_at, now),  # held-so-far for open rows
        "realized_pnl_dollars": realized_pnl_dollars,
        "unrealized_pnl_dollars": unrealized_pnl_dollars,
        "pnl_dollars": unrealized_pnl_dollars,
    }


def normalize_positions(
    closed_positions: list[dict],
    open_positions: list[dict],
    metadata_lookup: dict[str, dict],
    opened_at_lookup: dict[str, str],
    now: datetime,
    *,
    basis: str = "net",
) -> list[dict]:
    """Produce one unified trade row per position (closed first, then open)."""
    rows: list[dict] = [
        _normalize_closed_position(closed_position, metadata_lookup, basis=basis)
        for closed_position in closed_positions
    ]
    rows.extend(
        _normalize_open_position(open_position, metadata_lookup, opened_at_lookup, now, basis=basis)
        for open_position in open_positions
    )
    return rows


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_pnl_over_time(trade_rows: list[dict]) -> tuple[list[dict], float]:
    """Cumulative realized P&L curve over closed trades, plus current total incl. open.

    Returns ``(series, current_total_pnl_dollars)`` where ``series`` is ordered by
    ``closed_at`` and each point carries the running cumulative realized P&L and the
    running drawdown (cumulative minus running peak). ``current_total_pnl_dollars``
    is the grand total — all realized (closed + open locked-in) plus all open
    unrealized — used to draw the dashed "incl. open" marker.
    """
    closed_rows = [row for row in trade_rows if row["status"] == "closed"]
    closed_rows.sort(key=lambda row: row.get("closed_at") or "")

    series: list[dict] = []
    cumulative_pnl_dollars = 0.0
    running_peak_dollars = 0.0
    for row in closed_rows:
        cumulative_pnl_dollars += row["realized_pnl_dollars"]
        running_peak_dollars = max(running_peak_dollars, cumulative_pnl_dollars)
        series.append({
            "timestamp": row.get("closed_at"),
            "cumulative_pnl_dollars": round(cumulative_pnl_dollars, 2),
            "drawdown_dollars": round(cumulative_pnl_dollars - running_peak_dollars, 2),
        })

    total_realized_pnl_dollars = sum(row["realized_pnl_dollars"] for row in trade_rows)
    total_unrealized_pnl_dollars = sum(row["unrealized_pnl_dollars"] for row in trade_rows)
    current_total_pnl_dollars = round(total_realized_pnl_dollars + total_unrealized_pnl_dollars, 2)
    return series, current_total_pnl_dollars


def _max_drawdown_dollars(trade_rows: list[dict]) -> float:
    """Largest peak-to-trough decline on the closed-trade cumulative curve (<= 0)."""
    series, _ = compute_pnl_over_time(trade_rows)
    if not series:
        return 0.0
    return min(point["drawdown_dollars"] for point in series)


def compute_summary_metrics(trade_rows: list[dict]) -> dict:
    """Headline P&L figures plus closed-trade-only quality metrics."""
    closed_rows = [row for row in trade_rows if row["status"] == "closed"]

    realized_pnl_dollars = sum(row["realized_pnl_dollars"] for row in trade_rows)
    unrealized_pnl_dollars = sum(row["unrealized_pnl_dollars"] for row in trade_rows)

    closed_realized_pnl_dollars = sum(row["realized_pnl_dollars"] for row in closed_rows)
    winning_rows = [row for row in closed_rows if row["realized_pnl_dollars"] > 0]
    losing_rows = [row for row in closed_rows if row["realized_pnl_dollars"] < 0]

    gross_profit_dollars = sum(row["realized_pnl_dollars"] for row in winning_rows)
    gross_loss_dollars = sum(-row["realized_pnl_dollars"] for row in losing_rows)
    average_win_dollars = _mean([row["realized_pnl_dollars"] for row in winning_rows])
    average_loss_dollars = _mean([-row["realized_pnl_dollars"] for row in losing_rows])
    capital_deployed_dollars = sum(capital_for(row) for row in closed_rows)
    holding_days_values = [row["holding_days"] for row in closed_rows if row["holding_days"] is not None]

    closed_trade_count = len(closed_rows)
    return {
        "closed_trade_count": closed_trade_count,
        "open_trade_count": len(trade_rows) - closed_trade_count,
        "realized_pnl_dollars": round(realized_pnl_dollars, 2),
        "unrealized_pnl_dollars": round(unrealized_pnl_dollars, 2),
        "total_pnl_dollars": round(realized_pnl_dollars + unrealized_pnl_dollars, 2),
        "win_rate": round(len(winning_rows) / closed_trade_count, 4) if closed_trade_count else None,
        "winning_trade_count": len(winning_rows),
        "losing_trade_count": len(losing_rows),
        "average_win_dollars": round(average_win_dollars, 2) if average_win_dollars is not None else None,
        "average_loss_dollars": round(average_loss_dollars, 2) if average_loss_dollars is not None else None,
        "payoff_ratio": (
            round(average_win_dollars / average_loss_dollars, 2)
            if average_win_dollars is not None and average_loss_dollars not in (None, 0.0)
            else None
        ),
        "profit_factor": round(gross_profit_dollars / gross_loss_dollars, 2) if gross_loss_dollars > 0 else None,
        "expectancy_dollars": round(closed_realized_pnl_dollars / closed_trade_count, 2) if closed_trade_count else None,
        "capital_deployed_dollars": round(capital_deployed_dollars, 2),
        "realized_return_on_capital": (
            round(closed_realized_pnl_dollars / capital_deployed_dollars, 4)
            if capital_deployed_dollars > 0 else None
        ),
        "average_holding_days": round(_mean(holding_days_values), 2) if holding_days_values else None,
        "max_drawdown_dollars": round(_max_drawdown_dollars(trade_rows), 2),
    }


def _summarize_group(group_rows: list[dict]) -> dict:
    """Per-group P&L + closed-only ROI / win rate / avg holding, shared by both groupings."""
    closed_rows = [row for row in group_rows if row["status"] == "closed"]
    realized_pnl_dollars = sum(row["realized_pnl_dollars"] for row in group_rows)
    unrealized_pnl_dollars = sum(row["unrealized_pnl_dollars"] for row in group_rows)
    closed_realized_pnl_dollars = sum(row["realized_pnl_dollars"] for row in closed_rows)
    capital_deployed_dollars = sum(capital_for(row) for row in closed_rows)
    winning_rows = [row for row in closed_rows if row["realized_pnl_dollars"] > 0]
    holding_days_values = [row["holding_days"] for row in closed_rows if row["holding_days"] is not None]
    return {
        "closed_count": len(closed_rows),
        "open_count": len(group_rows) - len(closed_rows),
        "realized_pnl_dollars": round(realized_pnl_dollars, 2),
        "unrealized_pnl_dollars": round(unrealized_pnl_dollars, 2),
        "total_pnl_dollars": round(realized_pnl_dollars + unrealized_pnl_dollars, 2),
        "realized_return_on_capital": (
            round(closed_realized_pnl_dollars / capital_deployed_dollars, 4)
            if capital_deployed_dollars > 0 else None
        ),
        "win_rate": round(len(winning_rows) / len(closed_rows), 4) if closed_rows else None,
        "average_holding_days": round(_mean(holding_days_values), 2) if holding_days_values else None,
    }


def group_by_market_type(trade_rows: list[dict]) -> list[dict]:
    """Aggregate by market type, ordered by total P&L descending."""
    rows_by_market_type: dict[str, list[dict]] = {}
    for row in trade_rows:
        rows_by_market_type.setdefault(row["market_type"], []).append(row)

    grouped = [
        {"market_type": market_type, **_summarize_group(group_rows)}
        for market_type, group_rows in rows_by_market_type.items()
    ]
    grouped.sort(key=lambda group: group["total_pnl_dollars"], reverse=True)
    return grouped


def group_by_days_to_settlement(trade_rows: list[dict]) -> list[dict]:
    """Aggregate by days-to-settlement bucket, ordered by the fixed bucket sequence."""
    rows_by_bucket: dict[str, list[dict]] = {}
    for row in trade_rows:
        bucket_label = bucket_days_to_settlement(row["days_to_settlement"])
        rows_by_bucket.setdefault(bucket_label, []).append(row)

    bucket_bounds = {label: (lower, upper) for label, lower, upper in _DAYS_TO_SETTLEMENT_BUCKETS}
    ordered_labels = [label for label, _, _ in _DAYS_TO_SETTLEMENT_BUCKETS] + [_UNKNOWN_BUCKET_LABEL]

    grouped: list[dict] = []
    for bucket_label in ordered_labels:
        if bucket_label not in rows_by_bucket:
            continue
        lower_days, upper_days = bucket_bounds.get(bucket_label, (None, None))
        grouped.append({
            "bucket_label": bucket_label,
            "lower_days": lower_days,
            "upper_days": upper_days,
            **_summarize_group(rows_by_bucket[bucket_label]),
        })
    return grouped


def _round_trade_row(trade_row: dict) -> dict:
    """Round the dollar/day fields of a trade row for the JSON response."""
    rounded = dict(trade_row)
    for money_field in ("realized_pnl_dollars", "unrealized_pnl_dollars", "pnl_dollars"):
        rounded[money_field] = round(trade_row[money_field], 2)
    if trade_row.get("holding_days") is not None:
        rounded["holding_days"] = round(trade_row["holding_days"], 2)
    return rounded


def build_analytics(
    closed_positions: list[dict],
    open_positions: list[dict],
    metadata_lookup: dict[str, dict],
    opened_at_lookup: dict[str, str],
    now: datetime,
    *,
    basis: str = "net",
) -> dict:
    """Assemble the full ``/api/pnl/analytics`` payload."""
    trade_rows = normalize_positions(
        closed_positions, open_positions, metadata_lookup, opened_at_lookup, now, basis=basis
    )
    pnl_over_time, current_total_pnl_dollars = compute_pnl_over_time(trade_rows)
    return {
        "generated_at": now.isoformat(),
        "basis": basis,
        "summary": compute_summary_metrics(trade_rows),
        "pnl_over_time": pnl_over_time,
        "current_total_pnl_dollars": current_total_pnl_dollars,
        "by_market_type": group_by_market_type(trade_rows),
        "by_days_to_settlement": group_by_days_to_settlement(trade_rows),
        "trades": [_round_trade_row(row) for row in trade_rows],
    }
