"""Tests for kalshi_trader.ui.server FastAPI application."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalshi_trader.ui.config_manager import ConfigManager, DEFAULTS
from kalshi_trader.ui.state import TradingState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client(tmp_path: Path) -> tuple[TestClient, TradingState, ConfigManager]:
    """Return a fresh TestClient, TradingState, and ConfigManager."""
    # Import here so each test gets isolated objects
    from kalshi_trader.ui.server import create_app

    state = TradingState()
    mgr = ConfigManager(path=tmp_path / "cfg.json")
    app = create_app(trading_state=state, config_manager=mgr)
    client = TestClient(app)
    return client, state, mgr


# ---------------------------------------------------------------------------
# GET /api/state
# ---------------------------------------------------------------------------

class TestGetState:
    def test_returns_200(self, tmp_path):
        client, state, _ = make_client(tmp_path)
        resp = client.get("/api/state")
        assert resp.status_code == 200

    def test_body_matches_trading_state_defaults(self, tmp_path):
        client, state, _ = make_client(tmp_path)
        resp = client.get("/api/state")
        body = resp.json()
        expected = state.to_dict()
        assert body == expected

    def test_system_running_false_by_default(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        resp = client.get("/api/state")
        assert resp.json()["system_running"] is False

    def test_reflects_live_state_changes(self, tmp_path):
        client, state, _ = make_client(tmp_path)
        state.balance_dollars = 42.0
        resp = client.get("/api/state")
        assert resp.json()["balance_dollars"] == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------

class TestGetConfig:
    def test_returns_200(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        resp = client.get("/api/config")
        assert resp.status_code == 200

    def test_body_contains_all_default_keys(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        body = resp = client.get("/api/config").json()
        for key in DEFAULTS:
            assert key in body, f"Key '{key}' missing from /api/config response"

    def test_weight_noaa_default_value(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        body = client.get("/api/config").json()
        assert body["weight_noaa"] == pytest.approx(DEFAULTS["weight_noaa"])


# ---------------------------------------------------------------------------
# POST /api/config
# ---------------------------------------------------------------------------

class TestPostConfig:
    def test_valid_update_returns_200_ok(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        resp = client.post("/api/config", json={"weight_noaa": 0.5})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_valid_update_actually_updates_config(self, tmp_path):
        client, _, mgr = make_client(tmp_path)
        client.post("/api/config", json={"weight_noaa": 0.42})
        assert mgr.get("weight_noaa") == pytest.approx(0.42)

    def test_invalid_key_returns_422(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        resp = client.post("/api/config", json={"totally_fake_key": 99})
        assert resp.status_code == 422

    def test_invalid_key_body_contains_errors(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        resp = client.post("/api/config", json={"totally_fake_key": 99})
        body = resp.json()
        assert "errors" in body
        assert "totally_fake_key" in body["errors"]

    def test_out_of_range_value_returns_422(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        # weight_noaa max is 1.0
        resp = client.post("/api/config", json={"weight_noaa": 9.9})
        assert resp.status_code == 422
        assert "errors" in resp.json()

    def test_multiple_errors_all_reported(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        resp = client.post("/api/config", json={"fake_a": 1, "fake_b": 2})
        body = resp.json()
        assert resp.status_code == 422
        assert "fake_a" in body["errors"]
        assert "fake_b" in body["errors"]

    def test_invalid_update_does_not_change_config(self, tmp_path):
        client, _, mgr = make_client(tmp_path)
        original = mgr.get("weight_noaa")
        # One valid, one invalid — atomicity means nothing saves
        client.post("/api/config", json={"weight_noaa": 0.5, "bad_key": "oops"})
        assert mgr.get("weight_noaa") == original


# ---------------------------------------------------------------------------
# POST /api/system/start
# ---------------------------------------------------------------------------

class TestSystemStart:
    def test_start_returns_200_ok(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        resp = client.post("/api/system/start")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_start_sets_system_running_true(self, tmp_path):
        client, state, _ = make_client(tmp_path)
        client.post("/api/system/start")
        assert state.system_running is True

    def test_start_when_already_running_returns_409(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        client.post("/api/system/start")  # first start
        resp = client.post("/api/system/start")  # second start
        assert resp.status_code == 409
        assert resp.json() == {"error": "already running"}

    def test_start_creates_loop_task(self, tmp_path):
        from kalshi_trader.ui.server import create_app
        state = TradingState()
        mgr = ConfigManager(path=tmp_path / "cfg.json")
        app = create_app(trading_state=state, config_manager=mgr)
        client = TestClient(app)
        client.post("/api/system/start")
        assert app.state.loop_task is not None


# ---------------------------------------------------------------------------
# POST /api/system/stop
# ---------------------------------------------------------------------------

class TestSystemStop:
    def test_stop_when_running_returns_200_ok(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        client.post("/api/system/start")
        resp = client.post("/api/system/stop")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_stop_sets_system_running_false(self, tmp_path):
        client, state, _ = make_client(tmp_path)
        client.post("/api/system/start")
        assert state.system_running is True
        client.post("/api/system/stop")
        assert state.system_running is False

    def test_stop_when_not_running_returns_409(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        resp = client.post("/api/system/stop")
        assert resp.status_code == 409
        assert resp.json() == {"error": "not running"}

    def test_stop_clears_loop_task(self, tmp_path):
        from kalshi_trader.ui.server import create_app
        state = TradingState()
        mgr = ConfigManager(path=tmp_path / "cfg.json")
        app = create_app(trading_state=state, config_manager=mgr)
        client = TestClient(app)
        client.post("/api/system/start")
        assert app.state.loop_task is not None
        client.post("/api/system/stop")
        assert app.state.loop_task is None

    def test_stop_then_start_works(self, tmp_path):
        """After stopping, a second start should succeed (not 409)."""
        client, _, _ = make_client(tmp_path)
        client.post("/api/system/start")
        client.post("/api/system/stop")
        resp = client.post("/api/system/start")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
