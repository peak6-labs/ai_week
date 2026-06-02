"""Shared in-memory state between the FastAPI server and the trading loop.

Single asyncio event loop — no locks needed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass
class LogLine:
    timestamp: datetime
    message: str


@dataclass
class AgentStatus:
    enabled: bool = True
    status: Literal["idle", "running", "error"] = "idle"
    last_run_at: datetime | None = None
    last_signal_count: int = 0
    last_output_summary: dict[str, Any] = field(default_factory=dict)  # values must be JSON-serializable


@dataclass
class TradingState:
    system_running: bool = False
    cycle_number: int = 0
    last_cycle_at: datetime | None = None
    balance_dollars: float = 0.0
    daily_pnl_dollars: float = 0.0
    total_exposure_dollars: float = 0.0
    positions: list[dict] = field(default_factory=list)
    recent_ideas: list[dict] = field(default_factory=list)  # last 50 trade ideas, serializable dicts
    pending_ideas: list[dict] = field(default_factory=list)   # awaiting review
    reviewed_ideas: list[dict] = field(default_factory=list)  # last 50 reviewed, newest first
    agent_statuses: dict[str, AgentStatus] = field(default_factory=dict)
    event_log: deque[LogLine] = field(default_factory=lambda: deque(maxlen=200))
    last_error: str = ""

    def log(self, message: str) -> None:
        """Append a LogLine with the current UTC time to event_log."""
        self.event_log.append(LogLine(timestamp=datetime.now(tz=timezone.utc), message=message))

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation of the full state."""
        return {
            "system_running": self.system_running,
            "cycle_number": self.cycle_number,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at is not None else None,
            "balance_dollars": self.balance_dollars,
            "daily_pnl_dollars": self.daily_pnl_dollars,
            "total_exposure_dollars": self.total_exposure_dollars,
            "positions": self.positions,
            "recent_ideas": self.recent_ideas,
            "pending_ideas": list(self.pending_ideas),
            "reviewed_ideas": list(self.reviewed_ideas),
            "agent_statuses": {
                name: {
                    "enabled": agent.enabled,
                    "status": agent.status,
                    "last_run_at": agent.last_run_at.isoformat() if agent.last_run_at is not None else None,
                    "last_signal_count": agent.last_signal_count,
                    "last_output_summary": agent.last_output_summary,
                }
                for name, agent in self.agent_statuses.items()
            },
            "event_log": [
                {"timestamp": line.timestamp.isoformat(), "message": line.message}
                for line in self.event_log
            ],
            "last_error": self.last_error,
        }
