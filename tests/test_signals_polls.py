import pytest

from kalshi_trader.signals.polls import build_polls_signal, WEIGHT_FIVETHIRTYEIGHT


def _summary(margin, candidate_pct=51.0, opponent_pct=49.0, poll_count=8):
    return {
        "candidate": "Jon Ossoff",
        "candidate_pct": candidate_pct,
        "opponent": "Herschel Walker",
        "opponent_pct": opponent_pct,
        "margin": margin,
        "poll_count": poll_count,
    }


def test_build_polls_signal_basic_lead():
    sig = build_polls_signal(
        ticker="KXSENATEGA",
        margin_summary=_summary(margin=4.0),
        poll_type="senate",
        state="georgia",
    )
    assert sig.source == "fivethirtyeight"
    assert sig.weight == pytest.approx(WEIGHT_FIVETHIRTYEIGHT)
    assert sig.uncertainty == pytest.approx(0.10)
    # A +4 margin should give a win probability above 0.5.
    assert sig.probability > 0.5
    assert sig.metadata["data_quality"] == "fresh"
    assert sig.metadata["candidate"] == "Jon Ossoff"
    assert sig.metadata["state"] == "georgia"
    assert isinstance(sig.metadata["narrative"], str) and sig.metadata["narrative"]
    assert sig.data_issued_at.tzinfo is not None


def test_build_polls_signal_zero_margin_is_coinflip():
    sig = build_polls_signal("T", _summary(margin=0.0), poll_type="senate")
    assert sig.probability == pytest.approx(0.5, abs=0.02)


def test_build_polls_signal_trailing_candidate_below_half():
    sig = build_polls_signal("T", _summary(margin=-6.0), poll_type="senate")
    assert sig.probability < 0.5


def test_build_polls_signal_large_lead_high_prob():
    sig = build_polls_signal("T", _summary(margin=15.0), poll_type="president")
    assert sig.probability > 0.9


def test_build_polls_signal_probability_clamped():
    # Enormous margin → raw prob ~1.0, must clamp to <= 0.99.
    sig = build_polls_signal("T", _summary(margin=60.0), poll_type="president")
    assert sig.probability <= 0.99


def test_build_polls_signal_data_quality_thresholds():
    fresh = build_polls_signal("T", _summary(margin=2.0, poll_count=5), poll_type="senate")
    stale = build_polls_signal("T", _summary(margin=2.0, poll_count=2), poll_type="senate")
    unavailable = build_polls_signal("T", _summary(margin=2.0, poll_count=1), poll_type="senate")
    assert fresh.metadata["data_quality"] == "fresh"
    assert stale.metadata["data_quality"] == "stale"
    assert unavailable.metadata["data_quality"] == "unavailable"
