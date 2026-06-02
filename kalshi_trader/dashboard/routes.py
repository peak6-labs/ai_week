"""Read-only JSON API for the dashboard. Every route is GET.

- /api/health    scan freshness + environment
- /api/portfolio balance, exposure, PnL, positions (live)
- /api/orders    resting orders (live)
- /api/ideas     highest-scoring ideas (served from the in-memory scan cache)

The portfolio/orders routes fetch live account data through the dedicated live
client and join held/order tickers against market data (preferring the cached
scored slate, falling back to a live market lookup) for prices/categories/titles.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from kalshi_trader.dashboard import portfolio_mapping
from kalshi_trader.dashboard.state import DashboardState
from kalshi_trader.grouping import serialize_event_group
from kalshi_trader.models import Market

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _state(request: Request) -> DashboardState:
    return request.app.state.dashboard


def _age_seconds(moment: datetime | None) -> float | None:
    if moment is None:
        return None
    return round((datetime.now(timezone.utc) - moment).total_seconds(), 1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slate_status(state: DashboardState) -> str:
    if state.scored_slate_grouped is None:
        return "warming_up"
    return "degraded" if state.last_scan_error else "ready"


async def _resolve_markets(state: DashboardState, tickers: list[str]) -> dict[str, Market]:
    """Build a ticker -> Market lookup, preferring the cached slate and fetching
    only the tickers it doesn't already cover (held positions / order markets)."""
    lookup: dict[str, Market] = {}
    missing: list[str] = []
    for ticker in set(tickers):
        if not ticker:
            continue
        cached_market = state.scored_slate_markets.get(ticker)
        if cached_market is not None:
            lookup[ticker] = cached_market
        else:
            missing.append(ticker)

    async def _fetch(ticker: str) -> tuple[str, Market | None]:
        try:
            response = await state.live_client.get_market(ticker)
            return ticker, state.scanner._parse_market(response["market"])
        except Exception as caught_exception:
            _log.warning("Could not fetch market %s for join: %s", ticker, caught_exception)
            return ticker, None

    if missing:
        for ticker, market in await asyncio.gather(*[_fetch(ticker) for ticker in missing]):
            if market is not None:
                lookup[ticker] = market
    return lookup


@router.get("/health")
async def health(request: Request) -> dict:
    state = _state(request)
    return {
        "status": _slate_status(state),
        "kalshi_env": state.kalshi_env,
        "scan": {
            "last_generated_at": (
                state.scored_slate_generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if state.scored_slate_generated_at else None
            ),
            "cycle_number": state.scan_cycle_number,
            "in_progress": state.scan_in_progress,
            "last_error": state.last_scan_error,
            "age_seconds": _age_seconds(state.scored_slate_generated_at),
        },
    }


@router.get("/ideas")
async def ideas(request: Request, top: int = 10) -> dict:
    state = _state(request)
    grouped = state.scored_slate_grouped
    if grouped is None:
        return {"status": "warming_up", "generated_at": None, "age_seconds": None, "ideas": []}
    return {
        "status": _slate_status(state),
        "generated_at": (
            state.scored_slate_generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if state.scored_slate_generated_at else None
        ),
        "age_seconds": _age_seconds(state.scored_slate_generated_at),
        "ideas": [
            serialize_event_group(average_score, market_count, best_market)
            for average_score, market_count, best_market in grouped[:max(top, 0)]
        ],
    }


@router.get("/portfolio")
async def portfolio(request: Request) -> dict:
    state = _state(request)
    try:
        balance_raw, positions_response = await asyncio.gather(
            state.live_client.get_balance(),
            state.live_client.get_positions(),
        )
    except Exception as caught_exception:
        _log.warning("Portfolio fetch failed: %s", caught_exception)
        return {"status": "error", "error": repr(caught_exception), "as_of": _now_iso(), "positions": []}

    market_positions_raw = positions_response.get("market_positions") or []
    market_lookup = await _resolve_markets(
        state, [position.get("ticker", "") for position in market_positions_raw]
    )
    mapped_positions = portfolio_mapping.map_positions(market_positions_raw, market_lookup)

    response = {
        "status": "ready",
        "as_of": _now_iso(),
        **portfolio_mapping.map_balance(balance_raw),
        **portfolio_mapping.summarize_positions(mapped_positions),
        "positions": sorted(
            portfolio_mapping.open_positions(mapped_positions),
            key=lambda position: position["market_exposure_dollars"],
            reverse=True,
        ),
    }
    return response


@router.get("/orders")
async def orders(request: Request) -> dict:
    state = _state(request)
    try:
        orders_response = await state.live_client.get_orders(status="resting")
    except Exception as caught_exception:
        _log.warning("Orders fetch failed: %s", caught_exception)
        return {"status": "error", "error": repr(caught_exception), "as_of": _now_iso(), "orders": []}

    orders_raw = orders_response.get("orders") or []
    market_lookup = await _resolve_markets(
        state, [order.get("ticker", "") for order in orders_raw]
    )
    return {
        "status": "ready",
        "as_of": _now_iso(),
        "orders": portfolio_mapping.map_orders(orders_raw, market_lookup),
    }
