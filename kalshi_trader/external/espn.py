"""ESPN unofficial public API client — free sportsbook odds, no API key.

Used by the sportsbook signal: pull a game's moneyline (DraftKings / FanDuel via
ESPN), convert to a de-vigged implied probability, and compare to the Kalshi
price. These endpoints are undocumented and unauthenticated; we cache politely
and tolerate failures.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

try:  # route SSL through the system trust store (corporate proxy)
    import truststore
    truststore.inject_into_ssl()
except Exception:  # pragma: no cover
    pass

_log = logging.getLogger(__name__)

_SITE = "https://site.api.espn.com/apis/site/v2/sports"
_CORE = "https://sports.core.api.espn.com/v2/sports"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Kalshi-ish league key → (espn_sport, espn_league). Covers the leagues Kalshi
# lists most often.
LEAGUES: dict[str, tuple[str, str]] = {
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "ncaab": ("basketball", "mens-college-basketball"),
    "nfl": ("football", "nfl"),
    "ncaaf": ("football", "college-football"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "atp": ("tennis", "atp"),
    "wta": ("tennis", "wta"),
    "ufc": ("mma", "ufc"),
    "epl": ("soccer", "eng.1"),
    "mls": ("soccer", "usa.1"),
}


def american_to_prob(moneyline: float) -> float:
    """Convert American moneyline odds to implied probability in (0, 1)."""
    moneyline = float(moneyline)
    if moneyline < 0:
        return -moneyline / (-moneyline + 100.0)
    return 100.0 / (moneyline + 100.0)


def devig(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Remove the bookmaker's overround so two implied probabilities sum to 1."""
    total = prob_a + prob_b
    if total <= 0:
        return prob_a, prob_b
    return prob_a / total, prob_b / total


def _get(url: str, timeout: float = 20.0) -> dict[str, Any]:
    response = requests.get(url, headers=_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


class ESPNClient:
    """Thin synchronous client over ESPN's public endpoints."""

    def scoreboard(self, sport: str, league: str, date: str | None = None) -> list[dict]:
        """Return the events on the scoreboard (optionally for a YYYYMMDD date)."""
        url = f"{_SITE}/{sport}/{league}/scoreboard"
        if date:
            url += f"?dates={date}"
        try:
            return _get(url).get("events", []) or []
        except Exception as exc:
            _log.warning("ESPN scoreboard %s/%s failed: %s", sport, league, exc)
            return []

    def event_moneyline_probs(self, sport: str, league: str, event: dict) -> dict | None:
        """Return de-vigged moneyline implied probabilities for a scoreboard event.

        Prefers DraftKings/FanDuel; falls back to the first provider with a
        moneyline. Returns {home:{name,prob}, away:{name,prob}, book, source}
        or None when no moneyline is available.
        """
        competitions = event.get("competitions") or []
        if not competitions:
            return None
        competition = competitions[0]
        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            return None
        home_name = home.get("team", {}).get("displayName") or home.get("athlete", {}).get("displayName", "")
        away_name = away.get("team", {}).get("displayName") or away.get("athlete", {}).get("displayName", "")

        # The richer moneyline lives on the core odds endpoint.
        odds_items = self._core_odds(sport, league, event.get("id"), competition.get("id"))
        odds_items = odds_items or (competition.get("odds") or [])
        chosen = self._pick_provider(odds_items)
        if not chosen:
            return None
        home_ml = (chosen.get("homeTeamOdds") or {}).get("moneyLine")
        away_ml = (chosen.get("awayTeamOdds") or {}).get("moneyLine")
        if home_ml is None or away_ml is None:
            return None
        home_prob, away_prob = devig(american_to_prob(home_ml), american_to_prob(away_ml))
        return {
            "home": {"name": home_name, "prob": round(home_prob, 4)},
            "away": {"name": away_name, "prob": round(away_prob, 4)},
            "book": (chosen.get("provider") or {}).get("name", "unknown"),
            "details": chosen.get("details"),
        }

    def _core_odds(self, sport: str, league: str, event_id, comp_id) -> list[dict]:
        if not event_id or not comp_id:
            return []
        url = f"{_CORE}/{sport}/leagues/{league}/events/{event_id}/competitions/{comp_id}/odds"
        try:
            return _get(url).get("items", []) or []
        except Exception:
            return []

    @staticmethod
    def _pick_provider(odds_items: list[dict]) -> dict | None:
        if not odds_items:
            return None
        preferred = ("draftkings", "fanduel", "espn bet", "caesars")
        def has_ml(item: dict) -> bool:
            return (item.get("homeTeamOdds") or {}).get("moneyLine") is not None
        with_ml = [o for o in odds_items if has_ml(o)]
        if not with_ml:
            return None
        for name in preferred:
            for item in with_ml:
                if name in (item.get("provider") or {}).get("name", "").lower():
                    return item
        return with_ml[0]
