"""Post a log message to the trading dashboard UI.

Usage:
    python scripts/ui_log.py "message text"
    python scripts/ui_log.py "message text" warning
    python scripts/ui_log.py "message text" error

Level defaults to "info". Silently succeeds if the UI is not running.
"""
import json
import sys
import urllib.request

UI_LOG_URL = "http://localhost:8000/api/log"


def main() -> None:
    if len(sys.argv) < 2:
        return
    message = sys.argv[1]
    level = sys.argv[2] if len(sys.argv) > 2 else "info"
    if level not in ("info", "warning", "error"):
        level = "info"
    try:
        data = json.dumps({"message": message, "level": level}).encode()
        req = urllib.request.Request(
            UI_LOG_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # never break the calling pipeline


if __name__ == "__main__":
    main()
