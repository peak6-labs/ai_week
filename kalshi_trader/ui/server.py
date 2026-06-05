"""FastAPI server for the Kalshi trading dashboard.

Exposes REST endpoints for reading/writing state and config, and for
starting/stopping the trading loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from kalshi_trader.ui.state import TradingState
from kalshi_trader.ui.config_manager import cfg as _default_cfg, ConfigManager
from kalshi_trader.ui.pnl_analytics import build_analytics
from kalshi_trader.db import insert_reviewed_idea

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Slow-changing per-ticker data backing the P&L analytics tab. Maintained by the
# _poll_fills task and read by the /api/pnl/analytics endpoint. Kept at module
# level so the faster account poller (which rewrites trading_state.positions
# every 10s) can never clobber them.
_market_metadata_by_ticker: dict[str, dict] = {}   # ticker -> {"market_type", "close_time"}
_opened_at_by_ticker: dict[str, str] = {}           # ticker -> earliest buy-fill ISO time
_category_by_event_ticker: dict[str, str] = {}      # event_ticker -> category (deduped across markets)


def _avco_price(fill_list: list[dict], use_no_price: bool) -> float:
    total_weight = sum(fill.get("count", 0) for fill in fill_list)
    if total_weight == 0:
        return 0.0
    weighted_sum = sum(
        fill.get("count", 0) * (
            (100 - fill.get("yes_price", 0)) if use_no_price
            else fill.get("yes_price", 0)
        )
        for fill in fill_list
    )
    return weighted_sum / total_weight


def compute_closed_positions(
    fills_cache: dict[str, list[dict]],
    open_tickers: set[str],
) -> list[dict]:
    """Reconstruct closed positions from a per-ticker fills cache.

    A position is closed when total sell count >= total buy count for a ticker
    AND that ticker is not in the current open positions set. Returns rows
    sorted newest-closed-first, matching the shape expected by
    renderClosedPositions() in the UI.
    """
    result: list[dict] = []
    for ticker, fills in fills_cache.items():
        if ticker in open_tickers:
            continue
        buy_fills = [fill for fill in fills if fill.get("action") == "buy"]
        sell_fills = [fill for fill in fills if fill.get("action") == "sell"]
        total_bought = sum(fill.get("count", 0) for fill in buy_fills)
        total_sold = sum(fill.get("count", 0) for fill in sell_fills)
        if total_bought == 0 or total_sold < total_bought:
            continue

        side_raw = (buy_fills[0].get("side") or "yes").lower()
        side = "YES" if side_raw == "yes" else "NO"
        use_no_price = side == "NO"

        entry_price_cents = _avco_price(buy_fills, use_no_price)
        exit_price_cents = _avco_price(sell_fills, use_no_price)
        gross_realized_pnl_dollars = (exit_price_cents - entry_price_cents) * total_bought / 100.0
        total_fees = sum(fill.get("fee_cost", 0.0) for fill in fills)
        realized_pnl_dollars = gross_realized_pnl_dollars - total_fees

        opened_at = min(fill.get("created_time", "") for fill in buy_fills)
        closed_at = max(fill.get("created_time", "") for fill in sell_fills)

        result.append({
            "ticker": ticker,
            "side": side,
            "contracts": total_bought,
            "entry_price_cents": entry_price_cents,
            "exit_price_cents": exit_price_cents,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "gross_realized_pnl_dollars": round(gross_realized_pnl_dollars, 4),
            "realized_pnl_dollars": round(realized_pnl_dollars, 4),
        })

    result.sort(key=lambda row: row["closed_at"], reverse=True)
    return result


def merge_with_settled_positions(
    fills_based: list[dict],
    settled_api_positions: list[dict],
    fills_cache: dict[str, list[dict]],
) -> list[dict]:
    """Merge fills-based closed positions with the authoritative settled positions API.

    Settlement fills are often missing from /portfolio/fills, so fills-based
    detection alone misses positions that settled via market resolution. This
    function adds any settled positions not already covered by fills, using the
    API's realized_pnl_dollars as the authoritative figure, and enriches with
    entry price from fills where available.
    """
    fills_based_tickers = {row["ticker"] for row in fills_based}
    merged = list(fills_based)

    for position in settled_api_positions:
        ticker = position.get("ticker", "")
        if not ticker or ticker in fills_based_tickers:
            continue
        position_fp = float(position.get("position_fp", "0") or "0")
        if abs(position_fp) >= 1:
            continue
        realized_pnl = float(position.get("realized_pnl_dollars", "0") or "0")
        closed_at = position.get("last_updated_ts", "")

        ticker_fills = fills_cache.get(ticker, [])
        buy_fills = [f for f in ticker_fills if f.get("action") == "buy"]
        sell_fills = [f for f in ticker_fills if f.get("action") == "sell"]

        side_raw = (buy_fills[0].get("side") or "yes").lower() if buy_fills else "yes"
        side = "YES" if side_raw == "yes" else "NO"
        use_no_price = side == "NO"

        contracts = sum(f.get("count", 0) for f in buy_fills) or None
        entry_price_cents = _avco_price(buy_fills, use_no_price) if buy_fills else None
        exit_price_cents = _avco_price(sell_fills, use_no_price) if sell_fills else None
        opened_at = min(f.get("created_time", "") for f in buy_fills) if buy_fills else ""

        merged.append({
            "ticker": ticker,
            "side": side,
            "contracts": int(contracts) if contracts else None,
            "entry_price_cents": entry_price_cents,
            "exit_price_cents": exit_price_cents,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "gross_realized_pnl_dollars": round(realized_pnl, 4),
            "realized_pnl_dollars": round(realized_pnl, 4),
        })

    merged.sort(key=lambda row: row["closed_at"] or "", reverse=True)
    return merged


# ---------------------------------------------------------------------------
# Kalshi account poller — runs independently of the trading loop
# ---------------------------------------------------------------------------

async def _fetch_yes_price_data(client: Any, ticker: str, concurrency_semaphore: asyncio.Semaphore) -> dict | None:
    """Fetch YES bid, ask, and mid-price in cents for one ticker.

    Returns {"bid": float|None, "ask": float|None, "mid": float} or None on error.
    Prefers live bid/ask; falls back to last_price if no active quote.
    """
    async with concurrency_semaphore:
        try:
            response = await client.get_market(ticker)
            market_data = response.get("market", {})
            yes_bid = float(market_data.get("yes_bid", 0) or 0)
            yes_ask = float(market_data.get("yes_ask", 0) or 0)
            if yes_bid > 0 or yes_ask > 0:
                # Skip bid/ask mid when spread is the Kalshi default "no real market"
                # quote (bid ≤ 1¢, ask ≥ 99¢) — the 50¢ mid is meaningless there.
                if not (yes_bid <= 1 and yes_ask >= 99):
                    return {"bid": yes_bid, "ask": yes_ask, "mid": (yes_bid + yes_ask) / 2.0}
                last = market_data.get("last_price")
                return {"bid": yes_bid, "ask": yes_ask, "mid": float(last) if last is not None else None}
            last = market_data.get("last_price")
            if last is not None:
                return {"bid": None, "ask": None, "mid": float(last)}
        except Exception as caught_exception:
            logger.debug("Price data fetch failed for %s: %s", ticker, caught_exception)
        return None


async def _fetch_yes_price_cents(client: Any, ticker: str, concurrency_semaphore: asyncio.Semaphore) -> float | None:
    """Fetch the current YES mid-price in cents for one ticker.

    Prefers the live bid/ask midpoint over last_price. last_price is the most
    recent executed trade, which can be arbitrarily stale on illiquid markets
    while the bid/ask always reflects current quotes.
    """
    async with concurrency_semaphore:
        try:
            response = await client.get_market(ticker)
            market_data = response.get("market", {})
            yes_bid = float(market_data.get("yes_bid", 0) or 0)
            yes_ask = float(market_data.get("yes_ask", 0) or 0)
            if yes_bid > 0 or yes_ask > 0:
                return (yes_bid + yes_ask) / 2.0
            last = market_data.get("last_price")
            if last is not None:
                return float(last)
        except Exception as caught_exception:
            logger.debug("Price fetch failed for %s: %s", ticker, caught_exception)
        return None


async def _poll_kalshi_account(trading_state: TradingState) -> None:
    """Poll Kalshi every 10 s for balance, live position prices, unrealized P&L, and fees."""
    from kalshi_trader.client import KalshiClient
    from kalshi_trader.read_only_client import ReadOnlyKalshiClient

    await asyncio.sleep(2)
    trading_state.log("Account poller started")

    concurrency_semaphore = asyncio.Semaphore(5)

    async with KalshiClient() as raw_client:
        client = ReadOnlyKalshiClient(raw_client)
        while True:
            try:
                balance_resp, positions_resp, orders_resp = await asyncio.gather(
                    client.get_balance(),
                    client.get_positions(),
                    client.get_orders(status="resting"),
                )
                balance_cents = balance_resp.get("balance", 0) or 0
                trading_state.balance_dollars = balance_cents / 100.0

                raw_orders = orders_resp.get("orders") or []
                mapped_orders: list[dict] = []
                for raw_order in raw_orders:
                    remaining = float(raw_order.get("remaining_count_fp", "0") or 0)
                    if remaining <= 0:
                        continue
                    contract_side = raw_order.get("side") or raw_order.get("outcome_side") or "yes"
                    action = raw_order.get("action", "buy")
                    price_dollars = float(
                        raw_order.get("no_price_dollars") if contract_side == "no"
                        else (raw_order.get("yes_price_dollars") or 0)
                    )
                    mapped_orders.append({
                        "order_id": raw_order.get("order_id", ""),
                        "ticker": raw_order.get("ticker", ""),
                        "side": contract_side,
                        "action": action,
                        "price_cents": round(price_dollars * 100.0, 2),
                        "remaining": int(remaining),
                        "filled": int(float(raw_order.get("fill_count_fp", "0") or 0)),
                        "total": int(float(raw_order.get("initial_count_fp", "0") or 0)),
                        "created_time": raw_order.get("created_time"),
                    })

                # Enrich orders with live bid/ask and latest pipeline fair value.
                if mapped_orders:
                    order_tickers = list({o["ticker"] for o in mapped_orders if o["ticker"]})
                    order_price_results = await asyncio.gather(
                        *[_fetch_yes_price_data(client, ticker, concurrency_semaphore)
                          for ticker in order_tickers]
                    )
                    order_price_map: dict[str, dict | None] = dict(zip(order_tickers, order_price_results))

                    from kalshi_trader import db as _db
                    try:
                        fair_value_map = await _db.get_fair_values_from_recommendations(order_tickers)
                    except Exception as fair_value_exception:
                        logger.debug("Fair value lookup failed: %s", fair_value_exception)
                        fair_value_map = {}

                    for order in mapped_orders:
                        price_data = order_price_map.get(order["ticker"])
                        order["yes_bid"] = price_data["bid"] if price_data else None
                        order["yes_ask"] = price_data["ask"] if price_data else None
                        raw_prob = fair_value_map.get(order["ticker"])
                        order["fair_value_cents"] = round(raw_prob * 100, 1) if raw_prob is not None else None

                trading_state.orders = mapped_orders

                raw_positions = positions_resp.get("market_positions") or []
                held_raws = [p for p in raw_positions if float(p.get("position_fp", "0") or 0) != 0]

                tickers = [p.get("ticker", "") for p in held_raws]
                price_results = await asyncio.gather(
                    *[_fetch_yes_price_data(client, ticker, concurrency_semaphore) for ticker in tickers]
                )
                price_data_map: dict[str, dict | None] = dict(zip(tickers, price_results))

                position_fair_value_map: dict[str, float | None] = {}
                if tickers:
                    try:
                        position_fair_value_map = await _db.get_fair_values_from_recommendations(tickers)
                    except Exception as fair_value_exception:
                        logger.debug("Position fair value lookup failed: %s", fair_value_exception)

                total_exposure = 0.0
                total_unrealized_pnl = 0.0
                parsed: list[dict] = []
                for raw_position in held_raws:
                    qty_fp = float(raw_position.get("position_fp", "0") or 0)
                    qty = abs(qty_fp)
                    if int(qty) == 0:
                        continue
                    side = "YES" if qty_fp > 0 else "NO"
                    exposure = float(raw_position.get("market_exposure_dollars", "0") or 0)
                    fees = float(raw_position.get("fees_paid_dollars", "0") or 0)
                    realized = float(raw_position.get("realized_pnl_dollars", "0") or 0)
                    ticker = raw_position.get("ticker", "")

                    avg_price_cents = (exposure / qty * 100.0) if qty else 0.0

                    price_data = price_data_map.get(ticker)
                    yes_price = price_data["mid"] if price_data else None
                    current_price_cents: float | None = None
                    gross_unrealized_pnl: float | None = None
                    unrealized_pnl: float | None = None
                    if yes_price is not None and qty > 0:
                        current_price_cents = yes_price if side == "YES" else (100.0 - yes_price)
                        current_market_value = qty * current_price_cents / 100.0
                        gross_unrealized_pnl = current_market_value - exposure
                        unrealized_pnl = gross_unrealized_pnl - fees

                    total_exposure += exposure
                    if unrealized_pnl is not None:
                        total_unrealized_pnl += unrealized_pnl

                    parsed.append({
                        "ticker": ticker,
                        "side": side,
                        "quantity": int(qty),
                        "avg_price_dollars": round(avg_price_cents / 100.0, 4),
                        "current_price_dollars": round(current_price_cents / 100.0, 4) if current_price_cents is not None else None,
                        "yes_bid": price_data["bid"] if price_data else None,
                        "yes_ask": price_data["ask"] if price_data else None,
                        "fair_value_cents": round(position_fair_value_map[ticker] * 100, 1) if position_fair_value_map.get(ticker) is not None else None,
                        "total_cost_dollars": round(exposure, 2),
                        "total_cost_with_fees_dollars": round(exposure + fees, 2),
                        "gross_unrealized_pnl_dollars": round(gross_unrealized_pnl, 2) if gross_unrealized_pnl is not None else None,
                        "unrealized_pnl_dollars": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
                        "fees_paid_dollars": round(fees, 2),
                        "realized_pnl_dollars": round(realized, 2),
                    })

                trading_state.positions = parsed
                trading_state.total_exposure_dollars = total_exposure
                trading_state.daily_pnl_dollars = total_unrealized_pnl

            except asyncio.CancelledError:
                raise
            except Exception as caught_exception:
                logger.warning("Account poll failed: %s", caught_exception)

            await asyncio.sleep(10)


def _update_opened_at_lookup(fills_cache: dict[str, list[dict]]) -> None:
    """Record the earliest buy-fill time per ticker into _opened_at_by_ticker.

    Open positions have no opened_at natively, so the P&L tab derives it from the
    first buy fill. Closed positions already carry their own opened_at; this just
    backstops any ticker still trading.
    """
    for ticker, fills in fills_cache.items():
        buy_times = [
            fill.get("created_time") for fill in fills
            if fill.get("action") == "buy" and fill.get("created_time")
        ]
        if buy_times:
            _opened_at_by_ticker[ticker] = min(buy_times)


async def _refresh_market_metadata(client, tickers: set[str]) -> None:
    """Populate _market_metadata_by_ticker (category + close time) for new tickers.

    The single-market endpoint carries ``close_time`` but NOT ``category`` — on
    Kalshi the category lives on the event — so close time comes from
    ``get_market`` and the category from ``GET /events/{event_ticker}`` (cached
    per event so markets sharing an event cost one event call). Each ticker is
    fetched at most once; settled/closed markets never change. Runs the missing
    tickers concurrently under a small semaphore.
    """
    missing_tickers = [ticker for ticker in tickers if ticker and ticker not in _market_metadata_by_ticker]
    if not missing_tickers:
        return

    metadata_semaphore = asyncio.Semaphore(5)

    async def _category_for_event(event_ticker: str) -> str:
        if not event_ticker:
            return "unknown"
        if event_ticker in _category_by_event_ticker:
            return _category_by_event_ticker[event_ticker]
        try:
            response = await client.get(f"/events/{event_ticker}")
        except Exception as caught_exception:
            logger.debug("Event category fetch failed for %s: %s", event_ticker, caught_exception)
            return "unknown"
        event = response.get("event", response) or {}
        category = event.get("category") or "unknown"
        _category_by_event_ticker[event_ticker] = category
        return category

    async def _fetch_one(ticker: str) -> None:
        async with metadata_semaphore:
            try:
                response = await client.get_market(ticker)
            except Exception as caught_exception:
                logger.debug("Market metadata fetch failed for %s: %s", ticker, caught_exception)
                return
            market = response.get("market", response) or {}
            category = await _category_for_event(market.get("event_ticker", ""))
        _market_metadata_by_ticker[ticker] = {
            "market_type": category,
            "close_time": market.get("close_time"),
        }

    await asyncio.gather(*[_fetch_one(ticker) for ticker in missing_tickers])


async def _poll_fills(trading_state: TradingState) -> None:
    """Rebuild closed positions from Kalshi fills every 5 minutes.

    Fetches all portfolio fills via paginated GET /portfolio/fills, groups them
    by ticker, and calls compute_closed_positions() to derive the closed
    positions list. Replaces the Supabase-based _poll_closed_positions poller.
    """
    from kalshi_trader.client import KalshiClient
    from kalshi_trader.read_only_client import ReadOnlyKalshiClient

    await asyncio.sleep(4)
    trading_state.log("Fills poller started")

    while True:
        try:
            async with KalshiClient() as raw_client:
                client = ReadOnlyKalshiClient(raw_client)
                while True:
                    try:
                        fills_cache: dict[str, list[dict]] = {}
                        cursor: str | None = None

                        while True:
                            response = await client.get_fills(cursor=cursor)
                            page_fills = response.get("fills") or []
                            for fill in page_fills:
                                ticker = fill.get("ticker", "")
                                if not ticker:
                                    continue
                                # Normalize Kalshi API field names to internal format.
                                # Kalshi returns count_fp (float string), yes_price_dollars
                                # (dollar string), and fee_cost (dollar string).
                                normalized_fill = {
                                    "ticker": ticker,
                                    "side": fill.get("outcome_side") or fill.get("side") or "yes",
                                    "action": fill.get("action", ""),
                                    "count": int(float(fill.get("count_fp", "0") or "0")),
                                    "yes_price": round(float(fill.get("yes_price_dollars", "0") or "0") * 100),
                                    "fee_cost": float(fill.get("fee_cost", "0") or "0"),
                                    "created_time": fill.get("created_time", ""),
                                }
                                fills_cache.setdefault(ticker, []).append(normalized_fill)
                            next_cursor = response.get("cursor")
                            if not next_cursor or not page_fills:
                                break
                            cursor = next_cursor

                        open_tickers = {
                            p["ticker"] for p in trading_state.positions if p.get("ticker")
                        }
                        fills_based = compute_closed_positions(fills_cache, open_tickers)

                        settled_resp = await client.get(
                            "/portfolio/positions",
                            {"settlement_status": "settled", "limit": 200},
                        )
                        settled_positions = settled_resp.get("market_positions") or []
                        trading_state.closed_positions = merge_with_settled_positions(
                            fills_based, settled_positions, fills_cache
                        )

                        # Maintain the P&L-tab lookups: earliest buy time per
                        # ticker, and market metadata (category + close time).
                        _update_opened_at_lookup(fills_cache)
                        analytics_tickers = open_tickers | {
                            row["ticker"] for row in trading_state.closed_positions if row.get("ticker")
                        }
                        await _refresh_market_metadata(client, analytics_tickers)

                    except asyncio.CancelledError:
                        raise
                    except Exception as caught_exception:
                        logger.warning("Fills poll failed: %s", caught_exception)

                    await asyncio.sleep(300)  # 5 minutes
        except asyncio.CancelledError:
            raise
        except Exception as caught_exception:
            logger.warning("Fills poller client error, retrying in 60s: %s", caught_exception)
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    trading_state: TradingState | None = None,
    config_manager: ConfigManager | None = None,
) -> FastAPI:
    """Create and return a FastAPI application.

    Parameters
    ----------
    trading_state:
        Shared in-memory state object.  A fresh ``TradingState`` is created
        when *None*.
    config_manager:
        The ``ConfigManager`` to use for ``/api/config`` endpoints.  Defaults
        to the module-level singleton ``cfg``.
    """
    if trading_state is None:
        trading_state = TradingState()
    if config_manager is None:
        config_manager = _default_cfg

    app = FastAPI(title="Kalshi Trading Dashboard")

    # Attach shared objects to app state so route handlers can reach them.
    app.state.trading_state = trading_state
    app.state.config_manager = config_manager

    @app.on_event("startup")
    async def _start_account_poller() -> None:
        asyncio.create_task(_poll_kalshi_account(trading_state))
        asyncio.create_task(_poll_fills(trading_state))

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/")
    async def serve_index() -> FileResponse:
        """Serve the dashboard HTML."""
        index_path = _TEMPLATES_DIR / "index.html"
        return FileResponse(str(index_path))

    @app.get("/api/state")
    async def get_state(request: Request) -> JSONResponse:
        """Return the current TradingState as JSON."""
        state: TradingState = request.app.state.trading_state
        return JSONResponse(state.to_dict())

    @app.get("/api/pnl/analytics")
    async def get_pnl_analytics(request: Request) -> JSONResponse:
        """Return the P&L analysis payload for the P&L tab.

        Combines closed positions (realized) and open positions (mark-to-market
        unrealized + locked-in realized) into per-trade rows, summary metrics,
        a cumulative-P&L time series, and breakdowns by market type and
        days-to-settlement. ``?basis=gross`` switches off fee netting.
        """
        state: TradingState = request.app.state.trading_state
        basis = request.query_params.get("basis", "net")
        payload = build_analytics(
            state.closed_positions,
            state.positions,
            _market_metadata_by_ticker,
            _opened_at_by_ticker,
            datetime.now(tz=timezone.utc),
            basis=basis,
        )
        return JSONResponse(payload)

    @app.get("/api/config")
    async def get_config(request: Request) -> JSONResponse:
        """Return all runtime config values."""
        mgr: ConfigManager = request.app.state.config_manager
        return JSONResponse(mgr.all())

    @app.post("/api/config")
    async def post_config(request: Request) -> JSONResponse:
        """Validate and apply config updates.

        Expects a JSON object body.  Returns 422 + ``{"errors": {...}}`` on
        validation failure, or ``{"ok": true}`` on success.
        """
        mgr: ConfigManager = request.app.state.config_manager
        updates: dict[str, Any] = await request.json()
        errors = mgr.validate_and_update(updates)
        if errors:
            return JSONResponse({"errors": errors}, status_code=422)
        return JSONResponse({"ok": True})

    @app.post("/api/state")
    async def post_state(request: Request) -> JSONResponse:
        """Merge a partial state update pushed by the orchestrate pipeline.

        Accepts a JSON object with any subset of: ``cycle_number``,
        ``last_cycle_at``, ``daily_pnl_dollars``, ``positions``,
        ``recent_ideas``, ``agent_statuses``. Unknown keys are ignored. Always
        returns 200 — telemetry must never break the pipeline. (Balance and
        live positions are owned by the account poller; the pipeline should not
        push those.)
        """
        state: TradingState = request.app.state.trading_state
        try:
            body: dict = await request.json()
            if isinstance(body, dict):
                state.apply_update(body)
        except Exception:
            pass
        return JSONResponse({"ok": True})

    @app.post("/api/log")
    async def post_log(request: Request) -> JSONResponse:
        """Append a log line from an external agent or script.

        Accepts ``{"message": "...", "level": "info|warning|error"}``.
        Level defaults to "info". Always returns 200 — logging must never
        break the pipeline.
        """
        state: TradingState = request.app.state.trading_state
        try:
            body: dict = await request.json()
            message = str(body.get("message", ""))
            level = body.get("level", "info")
            if level not in ("info", "warning", "error"):
                level = "info"
            if message:
                state.log(message, level=level)
        except Exception:
            pass
        return JSONResponse({"ok": True})

    @app.get("/api/ideas/history")
    async def get_ideas_history() -> JSONResponse:
        """Return every recorded recommendation joined with its marks timeline.

        Read-only view of the paper-trade store: each idea carries its record-time
        fields (ticker, side, disposition, prices, edge, sources, status) plus an
        ordered ``marks`` list of (checked_at, current_value_cents, pnl_cents,
        resolved, elapsed_seconds) snapshots so the UI can plot how each idea
        moved over the intervals after it was presented. Never executes anything.
        """
        from kalshi_trader import db, paper

        try:
            # Primary source: Supabase, so any machine sees the shared data.
            ideas = await db.recommendations_with_marks()
        except Exception as supabase_exception:
            # Fall back to the local JSONL store when Supabase is unreachable.
            logger.warning("ideas history Supabase read failed, using local store: %s",
                           supabase_exception)
            try:
                ideas = paper.recommendations_with_marks()
            except Exception as local_exception:  # never let a bad store break the page
                logger.warning("ideas history local read failed: %s", local_exception)
                ideas = []
        return JSONResponse({"ideas": ideas})

    @app.post("/api/ideas")
    async def post_ideas(request: Request) -> JSONResponse:
        """Accept a list of idea dicts and append them to pending_ideas."""
        state: TradingState = request.app.state.trading_state
        ideas: list[dict] = await request.json()
        for idea in ideas:
            idea["id"] = str(uuid.uuid4())
        state.pending_ideas.extend(ideas)
        return JSONResponse({"ok": True, "count": len(ideas)})

    @app.post("/api/ideas/{idea_id}/approve")
    async def approve_idea(idea_id: str, request: Request) -> JSONResponse:
        """Approve a pending idea by id.

        Accepts an optional JSON body ``{"price_cents": float}`` to override
        the execution price.  Stored on the idea as ``override_price_cents``
        for the executor to use when execution is re-enabled.
        """
        state: TradingState = request.app.state.trading_state
        idx = next((i for i, idea in enumerate(state.pending_ideas) if idea.get("id") == idea_id), None)
        if idx is None:
            return JSONResponse({"error": "idea not found"}, status_code=404)
        idea = state.pending_ideas.pop(idx)
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("price_cents") is not None:
                idea["override_price_cents"] = float(body["price_cents"])
        except Exception:
            pass
        idea["decision"] = "approved"
        idea["reviewed_at"] = datetime.now(tz=timezone.utc).isoformat()
        state.reviewed_ideas.insert(0, idea)
        state.reviewed_ideas = state.reviewed_ideas[:50]
        try:
            await insert_reviewed_idea(idea, "approved")
        except Exception as exc:
            logger.error("DB save failed for approved idea %s: %s", idea_id, exc)
        return JSONResponse({"ok": True})

    @app.post("/api/ideas/{idea_id}/reject")
    async def reject_idea(idea_id: str, request: Request) -> JSONResponse:
        """Reject a pending idea by id."""
        state: TradingState = request.app.state.trading_state
        idx = next((i for i, idea in enumerate(state.pending_ideas) if idea.get("id") == idea_id), None)
        if idx is None:
            return JSONResponse({"error": "idea not found"}, status_code=404)
        idea = state.pending_ideas.pop(idx)
        idea["decision"] = "rejected"
        idea["reviewed_at"] = datetime.now(tz=timezone.utc).isoformat()
        state.reviewed_ideas.insert(0, idea)
        state.reviewed_ideas = state.reviewed_ideas[:50]
        try:
            await insert_reviewed_idea(idea, "rejected")
        except Exception as exc:
            logger.error("DB save failed for rejected idea %s: %s", idea_id, exc)
        return JSONResponse({"ok": True})

    @app.get("/api/markets/prices")
    async def get_market_prices(tickers: str = "") -> JSONResponse:
        """Return YES bid, ask, and mid-price in cents for each requested ticker.

        Accepts a comma-separated ``tickers`` query parameter (e.g.
        ``?tickers=FOO-1,BAR-2``).  Up to 20 tickers are fetched in parallel
        with a concurrency limit of 5.  Returns
        ``{ticker: {"bid": float|null, "ask": float|null, "mid": float} | null}``.
        """
        from kalshi_trader.client import KalshiClient
        from kalshi_trader.read_only_client import ReadOnlyKalshiClient

        if not tickers:
            return JSONResponse({})

        raw_tickers = [ticker.strip() for ticker in tickers.split(",") if ticker.strip()]
        unique_tickers = list(dict.fromkeys(raw_tickers))[:20]
        if not unique_tickers:
            return JSONResponse({})

        try:
            concurrency_semaphore = asyncio.Semaphore(5)
            async with KalshiClient() as raw_client:
                client = ReadOnlyKalshiClient(raw_client)
                price_results = await asyncio.gather(
                    *[_fetch_yes_price_data(client, ticker, concurrency_semaphore)
                      for ticker in unique_tickers]
                )
            return JSONResponse(dict(zip(unique_tickers, price_results)))
        except Exception as caught_exception:
            logger.warning("get_market_prices failed: %s", caught_exception)
            return JSONResponse({}, status_code=500)

    return app


# Module-level app instance for running directly (e.g. `uvicorn kalshi_trader.ui.server:app`).
app = create_app()
