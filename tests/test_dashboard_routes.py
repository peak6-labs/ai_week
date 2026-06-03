from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalshi_trader.dashboard.routes import router
from kalshi_trader.models import Market, ScanMetadata, ScoredMarket


def _market() -> Market:
    return Market(
        ticker="KXTEST-YES",
        event_ticker="KXTEST",
        series_ticker="KXTEST",
        title="Test market",
        yes_bid=48.0,
        yes_ask=50.0,
        last_price=49.0,
        volume_24h=1200,
        open_interest=2400,
        category="politics",
        close_time=datetime(2026, 7, 1, tzinfo=timezone.utc),
        status="open",
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    market = _market()
    scored = ScoredMarket(market=market, composite_score=0.7, volume_oi_ratio_score=0.5)
    metadata = ScanMetadata(
        live_prices_refreshed_at=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        shortlist_refreshed_at=datetime(2026, 6, 3, 12, 0, 30, tzinfo=timezone.utc),
        filtered_ticker_count=12,
        live_priced_ticker_count=10,
        dropped_unquoted_ticker_count=2,
        degraded=True,
        degraded_reason="Live pricing incomplete",
    )
    app.state.dashboard = SimpleNamespace(
        kalshi_env="prod",
        scored_slate_grouped=[(0.7, 1, scored)],
        scored_slate_markets={market.ticker: market},
        scored_slate_generated_at=datetime(2026, 6, 3, 12, 1, tzinfo=timezone.utc),
        scored_slate_metadata=metadata,
        last_scan_error="Live pricing incomplete",
        scan_in_progress=False,
        scan_cycle_number=4,
        live_client=None,
        scanner=None,
    )
    return TestClient(app)


def test_health_exposes_scan_metadata():
    response = _client().get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["scan"]["metadata"]["filtered_ticker_count"] == 12
    assert body["scan"]["metadata"]["live_priced_ticker_count"] == 10
    assert body["scan"]["metadata"]["dropped_unquoted_ticker_count"] == 2
    assert body["scan"]["metadata"]["degraded_reason"] == "Live pricing incomplete"


def test_ideas_exposes_scan_metadata():
    response = _client().get("/api/ideas?top=5")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["metadata"]["shortlist_refreshed_at"] == "2026-06-03T12:00:30Z"
    assert body["metadata"]["live_priced_ticker_count"] == 10
    assert len(body["ideas"]) == 1
