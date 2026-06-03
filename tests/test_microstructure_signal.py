"""Tests for the deterministic microstructure signal."""
from __future__ import annotations

from kalshi_trader.models import Candle
from kalshi_trader.signals import microstructure as ms


def _candle(ts: int, close: float) -> Candle:
    return Candle(end_period_ts=ts, volume=100, open_interest=1000,
                  price_open=close, price_high=close, price_low=close,
                  price_close=close, price_mean=close, price_previous=close)


def _rising() -> list[Candle]:
    return [_candle(i, 40 + i * 2) for i in range(4)]  # 40 -> 46, +6c


def _falling() -> list[Candle]:
    return [_candle(i, 50 - i * 2) for i in range(4)]


def test_signed_momentum_direction() -> None:
    assert ms.signed_momentum_cents(_rising()) > 0
    assert ms.signed_momentum_cents(_falling()) < 0
    assert ms.signed_momentum_cents([]) is None


def test_signed_ofi_direction() -> None:
    yes_heavy = [{"count_fp": 100, "taker_outcome_side": "yes"},
                 {"count_fp": 10, "taker_outcome_side": "no"}]
    assert ms.signed_ofi(yes_heavy) > 0
    assert ms.signed_ofi([]) is None


def test_signed_orderbook_skew_direction() -> None:
    book = {"yes": [[45, 1000], [44, 500]], "no": [[55, 50]]}
    skew = ms.signed_orderbook_skew(book)
    assert skew is not None and skew > 0  # more YES-side depth


def test_estimate_rising_momentum_pushes_yes() -> None:
    est = ms.build_microstructure_estimate(
        price_cents=45, hourly_candles=_rising(), daily_candles=[],
        trades=[{"count_fp": 80, "taker_outcome_side": "yes"}],
        orderbook={"yes": [[45, 800]], "no": [[55, 100]]}, ticker="T")
    assert est is not None
    assert est.source == "microstructure"
    assert est.metadata["direction"] == "yes"
    assert est.probability > 0.45


def test_estimate_none_when_no_data() -> None:
    est = ms.build_microstructure_estimate(
        price_cents=45, hourly_candles=[], daily_candles=[], trades=[],
        orderbook={}, ticker="T")
    assert est is None
