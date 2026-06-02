"""Tests for kalshi_trader.ui.state dataclasses."""

import json
from datetime import datetime, timezone

import pytest

from kalshi_trader.ui.state import AgentStatus, LogLine, TradingState


class TestTradingStateDefaults:
    def test_default_values(self):
        state = TradingState()
        assert state.system_running is False
        assert state.cycle_number == 0
        assert state.last_cycle_at is None
        assert state.balance_dollars == 0.0
        assert state.daily_pnl_dollars == 0.0
        assert state.total_exposure_dollars == 0.0
        assert state.positions == []
        assert state.recent_ideas == []
        assert state.agent_statuses == {}
        assert len(state.event_log) == 0
        assert state.last_error == ""

    def test_event_log_maxlen(self):
        state = TradingState()
        for i in range(250):
            state.log(f"message {i}")
        assert len(state.event_log) == 200


class TestLog:
    def test_log_appends_entry(self):
        state = TradingState()
        before = datetime.now(tz=timezone.utc)
        state.log("hello world")
        after = datetime.now(tz=timezone.utc)

        assert len(state.event_log) == 1
        line = state.event_log[0]
        assert isinstance(line, LogLine)
        assert line.message == "hello world"
        assert before <= line.timestamp <= after

    def test_log_uses_utc(self):
        state = TradingState()
        state.log("tz check")
        line = state.event_log[0]
        assert line.timestamp.tzinfo is not None
        assert line.timestamp.tzinfo == timezone.utc

    def test_log_multiple(self):
        state = TradingState()
        state.log("first")
        state.log("second")
        state.log("third")
        assert len(state.event_log) == 3
        assert [line.message for line in state.event_log] == ["first", "second", "third"]


class TestToDict:
    def test_json_serializable(self):
        state = TradingState()
        state.log("test message")
        state.agent_statuses["test_agent"] = AgentStatus(
            enabled=True,
            status="running",
            last_run_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            last_signal_count=5,
            last_output_summary={"signals": 5},
        )
        d = state.to_dict()
        # Should not raise
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_to_dict_structure(self):
        state = TradingState()
        d = state.to_dict()
        expected_keys = {
            "system_running", "cycle_number", "last_cycle_at",
            "balance_dollars", "daily_pnl_dollars", "total_exposure_dollars",
            "positions", "recent_ideas", "pending_ideas", "reviewed_ideas",
            "agent_statuses", "event_log", "last_error",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_last_cycle_at_none(self):
        state = TradingState()
        d = state.to_dict()
        assert d["last_cycle_at"] is None

    def test_to_dict_last_cycle_at_iso(self):
        dt = datetime(2026, 6, 1, 10, 30, 0, tzinfo=timezone.utc)
        state = TradingState(last_cycle_at=dt)
        d = state.to_dict()
        assert d["last_cycle_at"] == dt.isoformat()

    def test_to_dict_event_log_format(self):
        state = TradingState()
        state.log("event one")
        d = state.to_dict()
        assert len(d["event_log"]) == 1
        entry = d["event_log"][0]
        assert set(entry.keys()) == {"timestamp", "message"}
        assert entry["message"] == "event one"
        # timestamp must be a parseable ISO string
        parsed = datetime.fromisoformat(entry["timestamp"])
        assert parsed.tzinfo is not None

    def test_agent_status_serialization(self):
        state = TradingState()
        last_run = datetime(2026, 5, 15, 8, 0, 0, tzinfo=timezone.utc)
        state.agent_statuses["my_agent"] = AgentStatus(
            enabled=False,
            status="error",
            last_run_at=last_run,
            last_signal_count=3,
            last_output_summary={"foo": "bar"},
        )
        d = state.to_dict()
        agent_d = d["agent_statuses"]["my_agent"]
        assert agent_d["enabled"] is False
        assert agent_d["status"] == "error"
        assert agent_d["last_run_at"] == last_run.isoformat()
        assert agent_d["last_signal_count"] == 3
        assert agent_d["last_output_summary"] == {"foo": "bar"}

    def test_agent_status_last_run_at_none(self):
        state = TradingState()
        state.agent_statuses["idle_agent"] = AgentStatus()
        d = state.to_dict()
        assert d["agent_statuses"]["idle_agent"]["last_run_at"] is None

    def test_json_serializable_with_positions_and_ideas(self):
        state = TradingState(
            system_running=True,
            cycle_number=42,
            balance_dollars=1500.0,
            positions=[{"market": "X", "qty": 10, "side": "yes"}],
            recent_ideas=[{"market": "Y", "confidence": 0.75}],
        )
        state.log("startup")
        d = state.to_dict()
        serialized = json.dumps(d)
        assert '"system_running": true' in serialized
        assert '"cycle_number": 42' in serialized
