"""Push a partial state update to the trading dashboard UI.

Usage:
    # From a JSON string:
    python scripts/ui_state.py '{"cycle_number": 3, "last_cycle_at": "2026-06-02T18:00:00Z"}'

    # From a file:
    python scripts/ui_state.py --file /tmp/state_update.json

Merges any subset of: cycle_number, last_cycle_at, daily_pnl_dollars,
positions, recent_ideas, agent_statuses. Unknown keys are ignored by the
server. Silently succeeds if the UI is not running — telemetry must never break
the calling pipeline.
"""
import json
import sys
import urllib.request
from pathlib import Path

UI_STATE_URL = "http://localhost:8000/api/state"


def main() -> None:
    if len(sys.argv) < 2:
        return
    if sys.argv[1] == "--file":
        if len(sys.argv) < 3:
            return
        payload = Path(sys.argv[2]).read_text()
    else:
        payload = sys.argv[1]

    try:
        # Validate it is JSON before sending; bad input is a no-op, not a crash.
        json.loads(payload)
    except (ValueError, TypeError):
        return

    try:
        request = urllib.request.Request(
            UI_STATE_URL,
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=3)
    except Exception:
        pass  # never break the calling pipeline


if __name__ == "__main__":
    main()
