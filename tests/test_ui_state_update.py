"""Tests for TradingState.apply_update and the POST /api/state endpoint.

These cover the path the orchestrate pipeline uses to populate the dashboard
(cycle progress, per-agent status, recent ideas) via scripts/ui_state.py.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_trader.ui.config_manager import ConfigManager
from kalshi_trader.ui.state import TradingState


def make_client(tmp_path: Path) -> tuple[TestClient, TradingState]:
    from kalshi_trader.ui.server import create_app

    state = TradingState()
    mgr = ConfigManager(path=tmp_path / "cfg.json")
    app = create_app(trading_state=state, config_manager=mgr)
    return TestClient(app), state


class TestApplyUpdate:
    def test_scalar_fields_merge(self) -> None:
        state = TradingState()
        state.apply_update({"cycle_number": 4, "total_exposure_dollars": 120.5})
        assert state.cycle_number == 4
        assert state.total_exposure_dollars == 120.5

    def test_last_cycle_at_parses_iso_with_z(self) -> None:
        state = TradingState()
        state.apply_update({"last_cycle_at": "2026-06-02T18:00:00Z"})
        assert state.last_cycle_at is not None
        assert state.last_cycle_at.year == 2026

    def test_bad_timestamp_becomes_none_not_error(self) -> None:
        state = TradingState()
        state.apply_update({"last_cycle_at": "not-a-date"})
        assert state.last_cycle_at is None

    def test_unknown_keys_are_ignored(self) -> None:
        state = TradingState()
        state.apply_update({"totally_made_up": 99, "cycle_number": 1})
        assert not hasattr(state, "totally_made_up")
        assert state.cycle_number == 1

    def test_agent_statuses_build_objects(self) -> None:
        state = TradingState()
        state.apply_update({
            "agent_statuses": {
                "kalshi-bias-signal": {"status": "running", "last_signal_count": 2},
            }
        })
        agent = state.agent_statuses["kalshi-bias-signal"]
        assert agent.status == "running"
        assert agent.last_signal_count == 2

    def test_agent_statuses_merge_across_calls(self) -> None:
        state = TradingState()
        state.apply_update({"agent_statuses": {"a": {"status": "idle"}}})
        state.apply_update({"agent_statuses": {"b": {"status": "running"}}})
        assert set(state.agent_statuses) == {"a", "b"}

    def test_recent_ideas_replaced(self) -> None:
        state = TradingState()
        state.apply_update({"recent_ideas": [{"ticker": "KXFOO"}]})
        assert state.recent_ideas == [{"ticker": "KXFOO"}]


class TestPostStateEndpoint:
    def test_returns_200_ok(self, tmp_path) -> None:
        client, _ = make_client(tmp_path)
        resp = client.post("/api/state", json={"cycle_number": 7})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_update_is_reflected_in_get_state(self, tmp_path) -> None:
        client, _ = make_client(tmp_path)
        client.post("/api/state", json={"cycle_number": 7})
        body = client.get("/api/state").json()
        assert body["cycle_number"] == 7

    def test_agent_status_round_trips_through_get_state(self, tmp_path) -> None:
        client, _ = make_client(tmp_path)
        client.post("/api/state", json={
            "agent_statuses": {"order-flow-signal": {"status": "running", "last_signal_count": 1}}
        })
        body = client.get("/api/state").json()
        assert body["agent_statuses"]["order-flow-signal"]["status"] == "running"

    def test_malformed_body_still_returns_200(self, tmp_path) -> None:
        client, _ = make_client(tmp_path)
        resp = client.post("/api/state", content=b"not json",
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
