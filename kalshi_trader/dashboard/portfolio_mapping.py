"""Map raw Kalshi prod JSON into the dashboard's response dicts.

Pure functions (no I/O) so they're trivially unit-testable. The route layer does
the fetching and the market-data join, then calls these.

Field facts (from documentation/openapi.yaml), all confirmed against the prod schema:
- GetBalanceResponse: ``balance`` & ``portfolio_value`` are integer CENTS;
  ``balance_dollars`` is a fixed-point dollar string.
- MarketPosition: every ``*_dollars`` value is a fixed-point dollar STRING;
  ``position_fp`` is a signed contract-count string (negative = NO, positive = YES);
  there is NO side / current price / category / close time — those are joined from
  market data. ``realized_pnl_dollars`` is LIFETIME locked-in PnL, not daily.
- Order: prices are ``yes_price_dollars`` / ``no_price_dollars`` strings — pick by
  ``outcome_side``; counts are ``*_fp`` strings; ``created_time`` is an ISO string.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kalshi_trader.config import MAX_TOTAL_EXPOSURE_DOLLARS
from kalshi_trader.models import Market
from kalshi_trader.web_links import kalshi_market_url

EXPOSURE_LIMIT_DOLLARS: float = float(MAX_TOTAL_EXPOSURE_DOLLARS)


def parse_fixed_point(value: Any) -> float:
    """Parse a Kalshi fixed-point string (dollars or contract count) to float."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso_utc(value: Any) -> str | None:
    """Normalize a datetime or ISO string to UTC ISO-8601 with a trailing Z."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _current_yes_price_cents(market: Market | None) -> float | None:
    """Best estimate of the current YES price in cents from joined market data.

    Prefers the live bid/ask midpoint over last_price. last_price is the most
    recent executed trade, which can be arbitrarily stale on illiquid markets
    while the bid/ask always reflects current quotes.
    """
    if market is None:
        return None
    midpoint = (market.yes_bid + market.yes_ask) / 2.0
    if midpoint > 0:
        return midpoint
    if market.last_price:
        return float(market.last_price)
    return None


def map_balance(balance_raw: dict) -> dict:
    """Balance + portfolio value, normalized to dollars."""
    balance_cents = balance_raw.get("balance")
    portfolio_value_cents = balance_raw.get("portfolio_value")
    return {
        "balance_dollars": (
            float(balance_cents) / 100.0 if balance_cents is not None
            else parse_fixed_point(balance_raw.get("balance_dollars"))
        ),
        "portfolio_value_dollars": (
            float(portfolio_value_cents) / 100.0 if portfolio_value_cents is not None else None
        ),
        "updated_at": _iso_utc(balance_raw.get("updated_ts") and
                               datetime.fromtimestamp(balance_raw["updated_ts"], tz=timezone.utc)),
    }


def map_position(position_raw: dict, market_lookup: dict[str, Market]) -> dict:
    """Map one MarketPosition, joining current price / category / close time."""
    ticker = position_raw.get("ticker", "")
    signed_quantity = parse_fixed_point(position_raw.get("position_fp"))
    side = "yes" if signed_quantity >= 0 else "no"
    quantity = abs(signed_quantity)

    market_exposure_dollars = parse_fixed_point(position_raw.get("market_exposure_dollars"))
    realized_pnl_dollars = parse_fixed_point(position_raw.get("realized_pnl_dollars"))
    fees_paid_dollars = parse_fixed_point(position_raw.get("fees_paid_dollars"))

    # Average cost basis per contract, in this side's own price units (cents).
    avg_price_cents: float | None = (
        market_exposure_dollars / quantity * 100.0 if quantity > 0 else None
    )

    market = market_lookup.get(ticker)
    yes_price_cents = _current_yes_price_cents(market)
    current_price_cents: float | None = None
    unrealized_pnl_dollars: float | None = None
    if yes_price_cents is not None and quantity > 0:
        # A NO contract's price is the complement of the YES price.
        current_price_cents = yes_price_cents if side == "yes" else (100.0 - yes_price_cents)
        current_market_value_dollars = quantity * current_price_cents / 100.0
        unrealized_pnl_dollars = current_market_value_dollars - market_exposure_dollars - fees_paid_dollars

    return {
        "ticker": ticker,
        "title": market.title if market else None,
        "side": side,
        "quantity": quantity,
        "avg_price_cents": round(avg_price_cents, 2) if avg_price_cents is not None else None,
        "current_price_cents": round(current_price_cents, 2) if current_price_cents is not None else None,
        "market_exposure_dollars": round(market_exposure_dollars, 2),
        "unrealized_pnl_dollars": round(unrealized_pnl_dollars, 2) if unrealized_pnl_dollars is not None else None,
        "realized_pnl_dollars": round(realized_pnl_dollars, 2),
        "fees_paid_dollars": round(fees_paid_dollars, 2),
        "category": market.category if market else "unknown",
        "close_time": _iso_utc(market.close_time) if market else None,
        "kalshi_url": kalshi_market_url(ticker) if ticker else None,
    }


def map_positions(market_positions_raw: list[dict], market_lookup: dict[str, Market]) -> list[dict]:
    """Map all positions (including fully-closed quantity==0 ones)."""
    return [map_position(position_raw, market_lookup) for position_raw in market_positions_raw]


def open_positions(mapped_positions: list[dict]) -> list[dict]:
    """Just the positions still held (quantity != 0)."""
    return [position for position in mapped_positions if position["quantity"] != 0]


def summarize_positions(mapped_positions: list[dict]) -> dict:
    """Aggregate exposure / PnL across positions. Realized includes closed ones."""
    held = open_positions(mapped_positions)
    total_exposure_dollars = sum(position["market_exposure_dollars"] for position in held)
    realized_pnl_dollars = sum(position["realized_pnl_dollars"] for position in mapped_positions)
    unrealized_pnl_dollars = sum(
        position["unrealized_pnl_dollars"] or 0.0 for position in held
    )
    total_fees_paid_dollars = sum(position["fees_paid_dollars"] for position in held)
    exposure_by_category: dict[str, float] = {}
    for position in held:
        category = position["category"] or "unknown"
        exposure_by_category[category] = round(
            exposure_by_category.get(category, 0.0) + position["market_exposure_dollars"], 2
        )
    return {
        "total_exposure_dollars": round(total_exposure_dollars, 2),
        "exposure_limit_dollars": EXPOSURE_LIMIT_DOLLARS,
        "realized_pnl_dollars": round(realized_pnl_dollars, 2),
        "unrealized_pnl_dollars": round(unrealized_pnl_dollars, 2),
        "total_fees_paid_dollars": round(total_fees_paid_dollars, 2),
        "open_positions_count": len(held),
        "exposure_by_category": exposure_by_category,
    }


def map_order(order_raw: dict, market_lookup: dict[str, Market]) -> dict:
    """Map one resting Order. Price is chosen to match outcome_side."""
    ticker = order_raw.get("ticker", "")
    outcome_side = order_raw.get("outcome_side") or order_raw.get("side") or "yes"
    price_dollars = parse_fixed_point(
        order_raw.get("no_price_dollars") if outcome_side == "no"
        else order_raw.get("yes_price_dollars")
    )
    market = market_lookup.get(ticker)
    return {
        "order_id": order_raw.get("order_id", ""),
        "ticker": ticker,
        "title": market.title if market else None,
        "outcome_side": outcome_side,
        "type": order_raw.get("type", ""),
        "status": order_raw.get("status", ""),
        "price_cents": round(price_dollars * 100.0, 2),
        "remaining_count": parse_fixed_point(order_raw.get("remaining_count_fp")),
        "initial_count": parse_fixed_point(order_raw.get("initial_count_fp")),
        "fill_count": parse_fixed_point(order_raw.get("fill_count_fp")),
        "created_time": _iso_utc(order_raw.get("created_time")),
        "kalshi_url": kalshi_market_url(ticker) if ticker else None,
    }


def map_orders(orders_raw: list[dict], market_lookup: dict[str, Market]) -> list[dict]:
    """Map orders and keep only those still resting (remaining_count > 0)."""
    mapped = [map_order(order_raw, market_lookup) for order_raw in orders_raw]
    return [order for order in mapped if order["remaining_count"] > 0]
