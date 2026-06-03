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
from kalshi_trader.db import insert_reviewed_idea

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Kalshi account poller — runs independently of the trading loop
# ---------------------------------------------------------------------------

async def _poll_kalshi_account(trading_state: TradingState) -> None:
    """Poll Kalshi every 30 s for balance and positions, update TradingState."""
    from kalshi_trader.client import KalshiClient

    await asyncio.sleep(2)  # brief delay so server is fully up first
    trading_state.log("Account poller started")

    async with KalshiClient() as client:
        while True:
            try:
                balance_resp = await client.get_balance()
                balance_cents = balance_resp.get("balance", 0) or 0
                trading_state.balance_dollars = balance_cents / 100.0

                positions_resp = await client.get_positions()
                raw_positions = positions_resp.get("market_positions") or []

                total_exposure = 0.0
                total_pnl = 0.0
                parsed: list[dict] = []
                for p in raw_positions:
                    qty_fp = float(p.get("position_fp", "0") or 0)
                    if qty_fp == 0:
                        continue
                    side = "YES" if qty_fp > 0 else "NO"
                    qty = abs(int(qty_fp))
                    exposure = float(p.get("market_exposure_dollars", "0") or 0)
                    pnl = float(p.get("realized_pnl_dollars", "0") or 0)
                    total_exposure += exposure
                    total_pnl += pnl
                    ticker = p.get("ticker", "")
                    parsed.append({
                        "ticker": ticker,
                        "side": side,
                        "quantity": qty,
                        "avg_price_dollars": round(exposure / qty, 4) if qty else 0,
                        "current_price_dollars": None,
                        "unrealized_pnl_dollars": pnl,
                    })

                trading_state.positions = parsed
                trading_state.total_exposure_dollars = total_exposure
                trading_state.daily_pnl_dollars = total_pnl

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Account poll failed: %s", exc)

            await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Trading loop placeholder
# ---------------------------------------------------------------------------

async def _trading_loop(trading_state: TradingState) -> None:
    """Placeholder trading loop.  The real loop will replace this later."""
    trading_state.log("trading loop started")
    while True:
        await asyncio.sleep(3600)


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
    app.state.loop_task: asyncio.Task | None = None
    app.state.config_manager = config_manager

    @app.on_event("startup")
    async def _start_account_poller() -> None:
        asyncio.create_task(_poll_kalshi_account(trading_state))

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
        """Approve a pending idea by id."""
        state: TradingState = request.app.state.trading_state
        idx = next((i for i, idea in enumerate(state.pending_ideas) if idea.get("id") == idea_id), None)
        if idx is None:
            return JSONResponse({"error": "idea not found"}, status_code=404)
        idea = state.pending_ideas.pop(idx)
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

    @app.post("/api/system/start")
    async def system_start(request: Request) -> JSONResponse:
        """Start the trading loop asyncio Task."""
        state: TradingState = request.app.state.trading_state

        if state.system_running:
            return JSONResponse({"error": "already running"}, status_code=409)

        state.system_running = True
        task = asyncio.create_task(_trading_loop(state))

        def _on_done(t: asyncio.Task) -> None:
            exc = t.exception() if not t.cancelled() else None
            if exc is not None:
                state.system_running = False
                state.log(f"ERROR: {exc}")

        task.add_done_callback(_on_done)
        request.app.state.loop_task = task
        return JSONResponse({"ok": True})

    @app.post("/api/system/stop")
    async def system_stop(request: Request) -> JSONResponse:
        """Cancel the trading loop asyncio Task."""
        state: TradingState = request.app.state.trading_state

        if not state.system_running:
            return JSONResponse({"error": "not running"}, status_code=409)

        existing: asyncio.Task | None = request.app.state.loop_task
        if existing is not None and not existing.done():
            existing.cancel()

        state.system_running = False
        request.app.state.loop_task = None
        return JSONResponse({"ok": True})

    return app


# Module-level app instance for running directly (e.g. `uvicorn kalshi_trader.ui.server:app`).
app = create_app()
