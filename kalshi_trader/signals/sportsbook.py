"""Sportsbook-odds signal — compare a Kalshi sports market to the DraftKings line.

Sportsbook moneylines are among the sharpest public probability estimates, and
they're *independent* of Kalshi's own price/microstructure — exactly the kind of
corroboration the other signals lack. We pull the line from ESPN's free API,
de-vig it to an implied probability, match it to the Kalshi market's YES outcome,
and emit it as a SignalEstimate (low uncertainty, high weight).

Coverage is best for two-competitor head-to-head markets (tennis, NBA, NHL, MLB,
soccer moneyline). When we can't confidently match, we return no signal.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from kalshi_trader.external.espn import ESPNClient, LEAGUES
from kalshi_trader.models import SignalEstimate
from kalshi_trader.ui.config_manager import cfg

_log = logging.getLogger(__name__)

# Ticker-prefix / keyword hints → league key in espn.LEAGUES.
_LEAGUE_HINTS = [
    ("wta", "wta"), ("atp", "atp"), ("nba", "nba"), ("wnba", "wnba"),
    ("nhl", "nhl"), ("mlb", "mlb"), ("nfl", "nfl"), ("ufc", "ufc"),
    ("ncaaf", "ncaaf"), ("ncaab", "ncaab"), ("epl", "epl"), ("mls", "mls"),
]

_STOPWORDS = {"will", "the", "win", "beat", "defeat", "vs", "v", "match", "game",
              "to", "a", "of", "in", "at", "and", "round", "final", "finals",
              "semifinal", "semifinals", "quarterfinal", "be", "first"}


def detect_league(ticker: str, title: str) -> str | None:
    """Best-effort league detection from the ticker prefix / title keywords."""
    text = f"{ticker} {title}".lower()
    for hint, league_key in _LEAGUE_HINTS:
        if hint in text:
            return league_key
    return None


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t and t not in _STOPWORDS and len(t) > 1}


def implied_yes_probability(title: str, probs: dict) -> tuple[float, str] | None:
    """Pick which competitor is the market's YES side and return (prob, name).

    The YES subject is the competitor whose name appears earliest in the title
    (e.g. "Will Kostyuk win the Kostyuk vs Andreeva ..." → Kostyuk). Requires a
    real token match; returns None if neither competitor can be located.
    """
    title_lower = title.lower()
    best = None  # (position, prob, name)
    for slot in ("home", "away"):
        side = probs.get(slot) or {}
        name = side.get("name", "")
        prob = side.get("prob")
        if not name or prob is None:
            continue
        positions = [m.start() for token in _name_tokens(name)
                     for m in [re.search(rf"\b{re.escape(token)}", title_lower)] if m]
        if not positions:
            continue
        first_pos = min(positions)
        if best is None or first_pos < best[0]:
            best = (first_pos, float(prob), name)
    if best is None:
        return None
    return best[1], best[2]


def build_estimate(ticker: str, title: str, yes_prob: float, book: str,
                   matched_name: str) -> SignalEstimate:
    return SignalEstimate(
        source="sportsbook",
        probability=round(float(yes_prob), 4),
        uncertainty=float(cfg.get("uncertainty_sportsbook")),
        weight=float(cfg.get("weight_sportsbook")),
        data_issued_at=datetime.now(tz=timezone.utc),
        metadata={
            "ticker": ticker,
            "narrative": f"{book} line implies {yes_prob*100:.1f}% for {matched_name} (de-vigged moneyline).",
            "data_quality": "fresh",
            "book": book,
            "matched_competitor": matched_name,
        },
    )


def sportsbook_signal(
    ticker: str,
    title: str,
    league: str | None = None,
    client: ESPNClient | None = None,
) -> SignalEstimate | None:
    """Fetch the sportsbook line for a Kalshi sports market and build the signal.

    Returns None when the league/event can't be identified or no moneyline
    exists — never guesses.
    """
    league = league or detect_league(ticker, title)
    if league not in LEAGUES:
        return None
    sport, espn_league = LEAGUES[league]
    client = client or ESPNClient()

    events = client.scoreboard(sport, espn_league)
    title_tokens = _name_tokens(title)
    for event in events:
        probs = client.event_moneyline_probs(sport, espn_league, event)
        if not probs:
            continue
        # Only consider events whose competitors actually appear in the title.
        event_tokens = _name_tokens(probs["home"]["name"]) | _name_tokens(probs["away"]["name"])
        if not (title_tokens & event_tokens):
            continue
        matched = implied_yes_probability(title, probs)
        if matched is None:
            continue
        yes_prob, matched_name = matched
        return build_estimate(ticker, title, yes_prob, probs["book"], matched_name)
    return None
