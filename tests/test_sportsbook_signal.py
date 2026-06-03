"""Tests for the sportsbook-odds signal (offline — no live ESPN calls)."""
from __future__ import annotations

from kalshi_trader.external import espn
from kalshi_trader.signals import sportsbook as sb


def test_american_to_prob() -> None:
    assert round(espn.american_to_prob(-185), 3) == 0.649
    assert round(espn.american_to_prob(154), 3) == 0.394
    assert round(espn.american_to_prob(100), 3) == 0.5


def test_devig_sums_to_one() -> None:
    a, b = espn.devig(0.649, 0.394)
    assert round(a + b, 6) == 1.0
    assert a > b  # favorite keeps the larger share


def test_detect_league_from_ticker_and_title() -> None:
    assert sb.detect_league("KXWTAMATCH-26JUN04KOSAND-KOS", "Will Kostyuk win") == "wta"
    assert sb.detect_league("KXNBAGAME-26", "Will the Knicks win") == "nba"
    assert sb.detect_league("KXWEATHER-X", "max temp above 80") is None


_PROBS = {"home": {"name": "San Antonio Spurs", "prob": 0.62},
          "away": {"name": "New York Knicks", "prob": 0.38},
          "book": "DraftKings"}


def test_match_picks_earliest_named_competitor_as_yes() -> None:
    # "Knicks" appears first → YES is the Knicks (away, 0.38).
    yes = sb.implied_yes_probability("Will the New York Knicks beat the San Antonio Spurs?", _PROBS)
    assert yes is not None
    prob, name = yes
    assert name == "New York Knicks"
    assert prob == 0.38


def test_match_returns_none_when_no_competitor_in_title() -> None:
    assert sb.implied_yes_probability("Will it rain tomorrow in Dallas?", _PROBS) is None


def test_build_estimate_shape() -> None:
    est = sb.build_estimate("KXNBA-1", "Will the Knicks win", 0.38, "DraftKings", "New York Knicks")
    assert est.source == "sportsbook"
    assert est.probability == 0.38
    assert est.weight > 0.5  # sharp signal → high weight
    assert est.metadata["book"] == "DraftKings"
