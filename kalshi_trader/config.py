import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader — does not overwrite vars already set in the environment."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

KALSHI_ENV = os.environ.get("KALSHI_ENV", "demo").lower()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Must match kalshi_auth.py BASE_URLS exactly
_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://external-api.kalshi.com/trade-api/v2",
}
_WS_URLS = {
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "prod": "wss://external-api.kalshi.com/trade-api/ws/v2",
}
KALSHI_BASE_URL = _BASE_URLS.get(KALSHI_ENV, _BASE_URLS["demo"])
KALSHI_WS_URL = _WS_URLS.get(KALSHI_ENV, _WS_URLS["demo"])

# Risk thresholds
MAX_SINGLE_POSITION_DOLLARS = 100
MIN_SINGLE_POSITION_DOLLARS = 10
MAX_SINGLE_TRADE_LOSS_DOLLARS = 50
MAX_TOTAL_EXPOSURE_DOLLARS = 400
MAX_PER_CATEGORY_EXPOSURE_DOLLARS = 250
MAX_OPEN_POSITIONS = 10
DAILY_LOSS_LIMIT_DOLLARS = 100
MIN_HOURS_BEFORE_SETTLEMENT = 2

# Agent models
SPECIALIST_MODEL = "claude-sonnet-4-6"
COORDINATOR_MODEL = "claude-opus-4-8"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Polymarket — unauthenticated public API with no published rate limit.
# Keep concurrent requests low to avoid IP bans. Overrideable via env var.
POLYMARKET_MAX_CONCURRENT = int(os.environ.get("POLYMARKET_MAX_CONCURRENT", "8"))

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_MODEL = "grok-3"
X_GROK_UNCERTAINTY_THRESHOLD = 0.15
X_MAX_CONCURRENT_SEARCHES = 3
X_GROK_SIGNAL_WEIGHT = 0.6
X_CLAUDE_SIGNAL_WEIGHT = 0.75
