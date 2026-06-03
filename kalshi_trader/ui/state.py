"""Shared in-memory state between the FastAPI server and the trading loop.

Single asyncio event loop — no locks needed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp string into an aware datetime, tolerantly.

    Accepts a ``datetime`` (returned as-is), an ISO string (``Z`` suffix
    allowed), or None/garbage (returns None). Never raises — the pipeline must
    not break the UI with a malformed timestamp.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@dataclass
class LogLine:
    timestamp: datetime
    message: str
    level: Literal["info", "warning", "error"] = "info"


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
    orders: list[dict] = field(default_factory=list)
    recent_ideas: list[dict] = field(default_factory=list)  # last 50 trade ideas, serializable dicts
    pending_ideas: list[dict] = field(default_factory=list)   # awaiting review
    reviewed_ideas: list[dict] = field(default_factory=list)  # last 50 reviewed, newest first
    agent_statuses: dict[str, AgentStatus] = field(default_factory=dict)
    event_log: deque[LogLine] = field(default_factory=lambda: deque(maxlen=200))
    last_error: str = ""

    def log(self, message: str, level: Literal["info", "warning", "error"] = "info") -> None:
        """Append a LogLine with the current UTC time to event_log."""
        self.event_log.append(LogLine(timestamp=datetime.now(tz=timezone.utc), message=message, level=level))

    def apply_update(self, partial: dict[str, Any]) -> None:
        """Merge a partial state dict pushed by an external pipeline.

        Only known keys are applied; unknown keys are ignored so the pipeline
        can never corrupt state with a typo. ``last_cycle_at`` accepts an ISO
        timestamp string (or None). ``agent_statuses`` accepts a mapping of
        agent name to a status dict and is merged per agent, so one batch update
        does not wipe agents updated in a previous batch.
        """
        scalar_fields = {
            "system_running", "cycle_number", "balance_dollars",
            "daily_pnl_dollars", "total_exposure_dollars", "last_error",
        }
        list_fields = {"positions", "orders", "recent_ideas", "pending_ideas", "reviewed_ideas"}

        for field_name, value in partial.items():
            if field_name in scalar_fields:
                setattr(self, field_name, value)
            elif field_name in list_fields and isinstance(value, list):
                setattr(self, field_name, value)
            elif field_name == "last_cycle_at":
                self.last_cycle_at = _parse_timestamp(value)
            elif field_name == "agent_statuses" and isinstance(value, dict):
                for agent_name, status in value.items():
                    self.agent_statuses[agent_name] = AgentStatus(
                        enabled=bool(status.get("enabled", True)),
                        status=status.get("status", "idle"),
                        last_run_at=_parse_timestamp(status.get("last_run_at")),
                        last_signal_count=int(status.get("last_signal_count", 0)),
                        last_output_summary=status.get("last_output_summary", {}) or {},
                    )

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
            "orders": self.orders,
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
                {"timestamp": line.timestamp.isoformat(), "message": line.message, "level": line.level}
                for line in self.event_log
            ],
            "last_error": self.last_error,
        }
