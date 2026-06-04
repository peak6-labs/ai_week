"""Tests for the mentions-signal suppression gate (audit B1 calibration fix).

The 2026-06-04 backtest showed a GDELT-only base rate has negative skill in every
band, so a GDELT-only read is a non-tradeable prior (require corpus-backing) —
flagged non-informative (uncertainty>=0.99) so the scorer drops it. The narrower
saturation band is the fallback gate when require-corpus is disabled. Corpus-backed
reads are exempt (the rate is attributed to the speaker).
"""
from __future__ import annotations

import kalshi_trader.signals.mentions as mentions_module
from kalshi_trader.signals.mentions import (
    UNCERTAINTY_GDELT_ONLY,
    WEIGHT_GDELT_ONLY,
    build_mentions_base_signal,
)


def _gdelt(fraction: float, period_count: int = 185) -> dict:
    return {"period_count": period_count, "fraction_with_mention": fraction,
            "mean_match_percent": 0.1, "max_match_percent": 0.5}


def test_gdelt_only_saturated_high_is_suppressed():
    estimate = build_mentions_base_signal(
        "KXFOO-1", "recession", ["CSPAN"], gdelt_base_rate=_gdelt(1.0), corpus=None,
    )
    assert estimate is not None
    assert estimate.uncertainty >= 0.99  # scorer auto-drops it
    assert estimate.metadata["suppressed"] is True


def test_gdelt_only_saturated_low_is_suppressed():
    estimate = build_mentions_base_signal(
        "KXFOO-2", "airball", ["CSPAN"], gdelt_base_rate=_gdelt(0.05), corpus=None,
    )
    assert estimate is not None
    assert estimate.uncertainty >= 0.99
    assert estimate.metadata["suppressed"] is True


def test_gdelt_only_mid_band_suppressed_when_require_corpus():
    # Backtest verdict: GDELT-only has negative skill even mid-band, so with the
    # default require-corpus regime a mid-band GDELT-only read is also suppressed.
    estimate = build_mentions_base_signal(
        "KXFOO-3", "stagflation", ["CSPAN"], gdelt_base_rate=_gdelt(0.31), corpus=None,
    )
    assert estimate is not None
    assert estimate.uncertainty >= 0.99
    assert estimate.metadata["suppressed"] is True


def test_gdelt_only_mid_band_emits_when_require_corpus_disabled(monkeypatch):
    # With require-corpus off, only the narrower saturation gate applies, so a
    # mid-band rare word still emits a (low-weight) tradeable read.
    monkeypatch.setattr(mentions_module, "MENTIONS_REQUIRE_CORPUS_BACKED", False)
    estimate = build_mentions_base_signal(
        "KXFOO-3b", "stagflation", ["CSPAN"], gdelt_base_rate=_gdelt(0.31), corpus=None,
    )
    assert estimate is not None
    assert estimate.metadata["suppressed"] is False
    assert estimate.uncertainty == UNCERTAINTY_GDELT_ONLY
    assert estimate.weight == WEIGHT_GDELT_ONLY
    assert 0.15 < estimate.probability < 0.85


def test_corpus_backed_extreme_is_not_suppressed():
    # 19/20 attributed transcripts -> high probability, but corpus-backed: real
    # speaker evidence, so it is NOT suppressed even at an extreme.
    estimate = build_mentions_base_signal(
        "KXFOO-4", "inflation", ["CSPAN"],
        gdelt_base_rate=_gdelt(0.99), corpus={"document_count": 20, "match_count": 19},
    )
    assert estimate is not None
    assert estimate.metadata["suppressed"] is False
    assert estimate.uncertainty < 0.99
    assert estimate.metadata["independent"] is True
