import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader — does not overwrite vars already set in the environment."""
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        variable_name, _, variable_value = line.partition("=")
        os.environ.setdefault(variable_name.strip(), variable_value.strip())


_load_dotenv()

KALSHI_ENV = os.environ.get("KALSHI_ENV", "demo").lower()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Order kill-switch: no code path may place a live order unless this is set.
# Defaults OFF so a scan/analysis cycle can never silently place orders — the
# operator opts in explicitly (KALSHI_ALLOW_ORDERS=1) when they intend to trade.
KALSHI_ALLOW_ORDERS = os.environ.get("KALSHI_ALLOW_ORDERS", "false").lower() in ("1", "true", "yes")

# Must match kalshi_auth.py BASE_URLS exactly
_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://external-api.kalshi.com/trade-api/v2",
}
_WS_URLS = {
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "prod": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
}
KALSHI_BASE_URL = _BASE_URLS.get(KALSHI_ENV, _BASE_URLS["demo"])
KALSHI_WS_URL = _WS_URLS.get(KALSHI_ENV, _WS_URLS["demo"])

# Risk thresholds — scaled for the ~$500 working bankroll.
# Single position capped at 5% of bankroll; total exposure at ~30%.
MAX_SINGLE_POSITION_DOLLARS = 25
MIN_SINGLE_POSITION_DOLLARS = 5
MAX_SINGLE_TRADE_LOSS_DOLLARS = 25
MAX_TOTAL_EXPOSURE_DOLLARS = 150
MAX_PER_CATEGORY_EXPOSURE_DOLLARS = 75
MAX_OPEN_POSITIONS = 10
DAILY_LOSS_LIMIT_DOLLARS = 50
MIN_HOURS_BEFORE_SETTLEMENT = 2
# Staking fraction applied to the full-Kelly optimum inside RiskManager. Quarter-
# Kelly by default — conservative given how noisy the signal probabilities are.
KELLY_FRACTION = 0.25
# Minimum spacing between consecutive order placements, to avoid 429 rate limits.
INTER_ORDER_DELAY_SECONDS = 1.0

# Agent models
SPECIALIST_MODEL = "claude-sonnet-4-6"
COORDINATOR_MODEL = "claude-opus-4-8"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Polymarket — unauthenticated public API with no published rate limit.
# Keep concurrent requests low to avoid IP bans. Overrideable via env var.
POLYMARKET_MAX_CONCURRENT = int(os.environ.get("POLYMARKET_MAX_CONCURRENT", "8"))
POLYMARKET_MARKETS_CSV = os.environ.get(
    "POLYMARKET_MARKETS_CSV",
    str(Path(__file__).parent.parent / "poly_data" / "data" / "markets_active_part.csv"),
)
# Enable composite 3-signal whale scorer (win rate + direction accuracy + evidence weight).
WHALE_SCORER_V2: bool = os.environ.get("WHALE_SCORER_V2", "true").lower() not in ("0", "false", "no")

# One free api.data.gov key authorizes both congress.gov (hearing schedules) and
# GovInfo (CREC/CHRG transcripts). Absent ⇒ those clients return [] and the system
# degrades gracefully (no schedule veto, no CREC corpus).
DATA_GOV_API_KEY = os.environ.get("DATA_GOV_API_KEY", "")

# Mentions saturation gate: a GDELT-only base rate at/above HIGH or at/below LOW
# measures how ubiquitous a word is on TV, not whether a specific speaker says it
# in a single event — non-discriminative. Such GDELT-only reads are suppressed
# (marked non-informative) so they cannot manufacture a tradeable edge. Seeded
# from the mentions backtest calibration; tune from the paper loop.
MENTIONS_SATURATION_HIGH = 0.85
MENTIONS_SATURATION_LOW = 0.15
# The mentions backtest (2026-06-04, 455 settled markets) showed GDELT-only reads
# have NEGATIVE skill in every probability band — Brier 0.40 > naive 0.25, hit-rate
# 0.51 < an always-"yes" baseline of 0.55, and inverted calibration ("trump" 99%→
# unsaid; "airball" 1%→said). National-TV-news frequency measures the wrong thing
# for a single event. So a GDELT-only read is a non-tradeable prior: only a
# corpus-backed (speaker-attributed) read emits a tradeable edge. Set False only if
# a future backtest shows GDELT-only earns its weight. See
# thoughts/shared/research/2026-06-04-mentions-signal-effectiveness.md.
MENTIONS_REQUIRE_CORPUS_BACKED = True

# YouTube Data API v3 key for the Love Island signal (Peacock teaser discovery +
# video metadata). Free, ~10k quota units/day. Absent ⇒ YouTubeClient returns
# empty and the Love Island signal degrades gracefully (X-only or no signal).
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"
# Agent Tools API (/v1/responses + x_search) requires a reasoning model; the old
# grok-3 live-search (/chat/completions + search_parameters) was deprecated (410).
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4.3")
X_GROK_UNCERTAINTY_THRESHOLD = 0.15
X_MAX_CONCURRENT_SEARCHES = 3
X_GROK_SIGNAL_WEIGHT = 0.6
X_CLAUDE_SIGNAL_WEIGHT = 0.75

# Supabase — project ai_week only (xhyqdrhrwgebidvsnwbx)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
