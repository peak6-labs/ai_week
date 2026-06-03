"""Deterministic market-microstructure signal — pure math, no LLM, always available.

Turns the market-scout's trading logic into a *directional* trade signal. The
actionability scorer measures signal *magnitude* (how active a market is, for
ranking); this module recovers *direction* (which way) from the same raw data:

- signed momentum   — recent price drift (continuation hypothesis)
- signed OFI        — net taker-initiated YES vs NO volume (buying/selling pressure)
- signed book skew  — resting depth imbalance near mid (which side is bid up)
- range position    — where price sits in its range; an extreme position on a
                      volume spike is a peak/exhaustion (mean-reversion hypothesis)

These are weak individually, so the signal carries honest (high) uncertainty and
a modest weight. Its predictive value is validated empirically by the paper-trade
tracker, and the weight is tuned from that record.
"""
from __future__ import annotations

from datetime import datetime, timezone

from kalshi_trader.models import Candle, SignalEstimate
from kalshi_trader.ui.config_manager import cfg


def signed_momentum_cents(hourly_candles: list[Candle]) -> float | None:
    """Signed price drift (cents) over the last 4 hourly candles. + = rising."""
    if len(hourly_candles) < 4:
        return None
    ordered = sorted(hourly_candles, key=lambda candle: candle.end_period_ts)[-4:]
    closes = [candle.price_close for candle in ordered if candle.price_close is not None]
    if len(closes) < 2:
        return None
    return closes[-1] - closes[0]


def signed_ofi(trades: list[dict]) -> float | None:
    """Net order-flow imbalance in [-1, 1]. + = net YES (buyer-initiated) volume."""
    if not trades:
        return None
    yes_volume = 0.0
    no_volume = 0.0
    for trade in trades:
        try:
            count = float(trade.get("count_fp", 0) or 0)
        except (ValueError, TypeError):
            continue
        side = trade.get("taker_outcome_side", "")
        if side == "yes":
            yes_volume += count
        elif side == "no":
            no_volume += count
    total = yes_volume + no_volume
    if total <= 0:
        return None
    return (yes_volume - no_volume) / total


def signed_orderbook_skew(orderbook: dict) -> float | None:
    """Resting-depth imbalance near mid in [-1, 1]. + = more YES-side depth."""
    yes_levels = orderbook.get("yes", []) or []
    no_levels = orderbook.get("no", []) or []
    if not yes_levels and not no_levels:
        return None

    def depth_near_mid(levels: list, cents_window: float = 5.0) -> float:
        prices = [float(level[0]) for level in levels
                  if isinstance(level, (list, tuple)) and len(level) >= 1]
        if not prices:
            return 0.0
        midpoint = (max(prices) + min(prices)) / 2.0
        total = 0.0
        for level in levels:
            try:
                price, size = float(level[0]), float(level[1])
            except (IndexError, TypeError, ValueError):
                continue
            if abs(price - midpoint) <= cents_window:
                total += size
        return total

    yes_depth = depth_near_mid(yes_levels)
    no_depth = depth_near_mid(no_levels)
    total = yes_depth + no_depth
    if total <= 0:
        return None
    return (yes_depth - no_depth) / total


def range_position(candles: list[Candle], current_price_cents: float) -> float | None:
    """Where price sits in its range: 0.0 = low, 1.0 = high. None if flat/empty."""
    if not candles:
        return None
    closes = [candle.price_close for candle in candles if candle.price_close is not None]
    closes.append(current_price_cents)
    if len(closes) < 2:
        return None
    price_low, price_high = min(closes), max(closes)
    if price_high - price_low < 2.0:
        return None
    return (current_price_cents - price_low) / (price_high - price_low)


# Component weights within the microstructure blend (sum need not be 1; we
# normalize over whichever components are present).
_COMPONENT_WEIGHTS = {"momentum": 0.35, "ofi": 0.30, "skew": 0.15, "reversion": 0.20}
_MAX_NUDGE = 0.08  # max probability shift (8¢) at full one-directional pressure


def estimate_from_signed(
    price_cents: float,
    momentum_cents: float | None,
    ofi: float | None,
    skew: float | None,
    position: float | None,
    ticker: str = "",
) -> SignalEstimate | None:
    """Blend precomputed signed components into a directional SignalEstimate.

    Used by the scout pass, which already has the signed components computed from
    the candle/trade/orderbook data it fetched once. Returns None when no
    components are available or the net nudge is negligible.
    """
    if price_cents <= 0 or price_cents >= 100:
        return None
    price_prob = price_cents / 100.0

    components: dict[str, float] = {}
    if momentum_cents is not None:
        components["momentum"] = max(-1.0, min(1.0, momentum_cents / 10.0))  # 10¢ = full
    if ofi is not None:
        components["ofi"] = ofi
    if skew is not None:
        components["skew"] = skew
    # Peak / exhaustion: an extreme range position implies mean-reversion.
    if position is not None:
        if position > 0.8:
            components["reversion"] = -((position - 0.8) / 0.2)  # near high → bearish
        elif position < 0.2:
            components["reversion"] = (0.2 - position) / 0.2      # near low → bullish

    if not components:
        return None

    total_weight = sum(_COMPONENT_WEIGHTS[name] for name in components)
    net_pressure = sum(_COMPONENT_WEIGHTS[name] * value for name, value in components.items()) / total_weight

    nudge = net_pressure * _MAX_NUDGE
    probability = max(0.02, min(0.98, price_prob + nudge))
    if abs(probability - price_prob) < 0.01:
        return None  # negligible

    direction = "yes" if nudge > 0 else "no"
    narrative = (
        f"Microstructure {direction} pressure ({net_pressure:+.2f}) from "
        + ", ".join(f"{name}={value:+.2f}" for name, value in components.items())
        + f"; {price_cents:.0f}¢ → {probability*100:.0f}¢."
    )
    return SignalEstimate(
        source="microstructure",
        probability=round(probability, 4),
        uncertainty=float(cfg.get("uncertainty_microstructure")),
        weight=float(cfg.get("weight_microstructure")),
        data_issued_at=datetime.now(tz=timezone.utc),
        metadata={
            "ticker": ticker,
            "narrative": narrative,
            "data_quality": "fresh",
            "components": {name: round(value, 4) for name, value in components.items()},
            "net_pressure": round(net_pressure, 4),
            "range_position": round(position, 4) if position is not None else None,
            "direction": direction,
        },
    )


def build_microstructure_estimate(
    price_cents: float,
    hourly_candles: list[Candle],
    daily_candles: list[Candle],
    trades: list[dict],
    orderbook: dict,
    ticker: str = "",
) -> SignalEstimate | None:
    """Compute signed components from raw data, then blend into an estimate."""
    return estimate_from_signed(
        price_cents=price_cents,
        momentum_cents=signed_momentum_cents(hourly_candles),
        ofi=signed_ofi(trades),
        skew=signed_orderbook_skew(orderbook),
        position=range_position(hourly_candles or daily_candles, price_cents),
        ticker=ticker,
    )
