"""FastAPI app for the read-only Kalshi portfolio dashboard.

READ-ONLY. This process runs against the prod (real-money) account and must never
place or cancel an order. Two guarantees enforce that:
  1. All Kalshi access goes through ReadOnlyKalshiClient (no write methods exist).
  2. ``_assert_read_only`` fails startup if any non-GET route is ever registered.

Run (from the repo root):
    KALSHI_ENV=prod .venv/bin/uvicorn kalshi_trader.dashboard.app:app \
        --host 127.0.0.1 --port 8000 --workers 1

Single worker is load-bearing: the in-memory scan cache and the background scoring
loop live in one process.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from kalshi_trader.dashboard.routes import router
from kalshi_trader.dashboard.scoring_loop import run_scoring_loop
from kalshi_trader.dashboard.state import create_dashboard_state

_log = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Surface kalshi_trader scan/loop logs (uvicorn only configures its own).

    Attaches one handler to the package logger with propagate disabled, so these
    lines don't duplicate through uvicorn's root configuration. Idempotent.
    """
    package_logger = logging.getLogger("kalshi_trader")
    if package_logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s — %(message)s", datefmt="%H:%M:%S"
    ))
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.INFO)
    package_logger.propagate = False

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _assert_read_only(app: FastAPI) -> None:
    """Fail loudly if any route can mutate. Read-only is non-negotiable here."""
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        forbidden = methods & _WRITE_METHODS
        if forbidden:
            raise RuntimeError(
                f"Read-only dashboard refuses to start: route {getattr(route, 'path', route)} "
                f"exposes write method(s) {sorted(forbidden)}. This dashboard must never "
                "place or cancel orders."
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = create_dashboard_state()
    app.state.dashboard = state
    _log.info("Dashboard starting — KALSHI_ENV=%s, READ-ONLY", state.kalshi_env)
    scan_task = asyncio.create_task(run_scoring_loop(state), name="scoring-loop")
    try:
        yield
    finally:
        scan_task.cancel()
        try:
            await scan_task
        except asyncio.CancelledError:
            pass
        state.close()
        _log.info("Dashboard stopped")


def create_app() -> FastAPI:
    _configure_logging()
    app = FastAPI(title="Kalshi Portfolio Monitor", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.include_router(router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_INDEX_HTML)

    _assert_read_only(app)
    return app


app = create_app()
