from datetime import datetime, timedelta
from kalshi_trader.models import SignalEstimate


def test_staleness_minutes_is_dynamic():
    issued = datetime.utcnow() - timedelta(minutes=30)
    est = SignalEstimate(
        source="noaa_gfs",
        probability=0.65,
        uncertainty=0.08,
        weight=0.85,
        data_issued_at=issued,
        metadata={},
    )
    assert 29 < est.staleness_minutes < 31


def test_staleness_increases_over_time():
    issued = datetime.utcnow() - timedelta(minutes=60)
    est = SignalEstimate(
        source="noaa_gfs",
        probability=0.65,
        uncertainty=0.08,
        weight=0.85,
        data_issued_at=issued,
        metadata={},
    )
    assert est.staleness_minutes > 59
