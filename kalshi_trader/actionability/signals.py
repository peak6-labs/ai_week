from __future__ import annotations

from kalshi_trader.models import Candle, Market


def volume_oi_ratio_score(market: Market) -> float:
    """Daily turnover rate: volume_24h / open_interest, capped at 50% = 1.0."""
    if market.open_interest <= 0:
        return 0.0
    ratio = market.volume_24h / market.open_interest
    return min(1.0, ratio / 0.50)


def relative_historical_volume_score(
    daily_candles: list[Candle],
    volume_24h: int,
) -> float | None:
    """Is today's volume unusual vs this market's own 30-day baseline?

    Returns None when fewer than 3 days of history are available.
    Score = 0.0 at baseline, 1.0 at 3× baseline.
    """
    volumes = [c.volume for c in daily_candles if c.volume > 0]
    if len(volumes) < 3:
        return None
    baseline = sum(volumes) / len(volumes)
    if baseline <= 0:
        return None
    ratio = volume_24h / baseline
    return min(1.0, max(0.0, (ratio - 1.0) / 2.0))


def volume_spike_short_term_score(hourly_candles: list[Candle]) -> float | None:
    """Is the last hour unusually active vs this market's average hourly volume?

    Requires at least 4 hourly candles (last 1h vs prior 3h+ baseline).
    Score = 0.0 at baseline, 1.0 at 2.5× baseline.
    """
    if len(hourly_candles) < 4:
        return None
    sorted_candles = sorted(hourly_candles, key=lambda c: c.end_period_ts)
    last = sorted_candles[-1]
    prior = sorted_candles[:-1]
    baseline_volumes = [c.volume for c in prior if c.volume >= 0]
    if not baseline_volumes:
        return None
    baseline = sum(baseline_volumes) / len(baseline_volumes)
    if baseline <= 0:
        return 0.0
    ratio = last.volume / baseline
    return min(1.0, max(0.0, (ratio - 1.0) / 1.5))


def oi_change_score(hourly_candles: list[Candle]) -> float | None:
    """Are new participants entering? 10% OI growth over 24h = full score.

    Returns None if fewer than 2 hourly candles are available.
    Shrinking or flat OI scores 0.0.
    """
    if len(hourly_candles) < 2:
        return None
    sorted_candles = sorted(hourly_candles, key=lambda c: c.end_period_ts)
    oi_now = sorted_candles[-1].open_interest
    oi_old = sorted_candles[0].open_interest
    if oi_old <= 0:
        return None
    delta_ratio = (oi_now - oi_old) / oi_old
    return min(1.0, max(0.0, delta_ratio / 0.10))


def momentum_score(hourly_candles: list[Candle]) -> float | None:
    """Is price moving with conviction over the last 4 hours?

    Uses trade price (price_close), skipping candles with no trades.
    10-cent move over 4h = full score.
    Returns None if fewer than 4 hourly candles available.
    """
    if len(hourly_candles) < 4:
        return None
    sorted_candles = sorted(hourly_candles, key=lambda c: c.end_period_ts)
    recent = sorted_candles[-4:]
    prices = [c.price_close for c in recent if c.price_close is not None]
    if len(prices) < 2:
        return 0.0
    delta = abs(prices[-1] - prices[0])
    return min(1.0, delta / 10.0)


def hl_position_score(candles: list[Candle], current_price: float) -> float | None:
    """Is price at an extreme of its range over the given candles?

    Score = 0 at midrange, 1 at high or low.
    Returns None if no candles; 0.5 if the range is flat.
    """
    if not candles:
        return None
    prices = [c.price_close for c in candles if c.price_close is not None]
    prices.append(current_price)
    if len(prices) < 2:
        return None
    lo = min(prices)
    hi = max(prices)
    if hi == lo:
        return 0.5
    position = (current_price - lo) / (hi - lo)
    return abs(position - 0.5) * 2.0


def ofi_score(trades: list[dict]) -> float | None:
    """Are trades directionally one-sided? (Order Flow Imbalance)

    Uses taker_outcome_side: "yes" = buyer-initiated, "no" = seller-initiated.
    Score = 0.0 when balanced, 1.0 when fully one-sided.
    Returns None if no trades.
    """
    if not trades:
        return None
    yes_vol = 0.0
    no_vol = 0.0
    for t in trades:
        try:
            count = float(t.get("count_fp", 0) or 0)
        except (ValueError, TypeError):
            continue
        side = t.get("taker_outcome_side", "")
        if side == "yes":
            yes_vol += count
        elif side == "no":
            no_vol += count
    total = yes_vol + no_vol
    if total <= 0:
        return None
    return abs(yes_vol - no_vol) / total


def orderbook_skew_score(orderbook: dict) -> float:
    """Is the order book lopsided within 5 cents of mid?

    Score = 0.0 when balanced, 1.0 when fully one-sided.
    Returns 0.5 (neutral) when orderbook data is empty or malformed.
    """
    yes_levels = orderbook.get("yes", []) or []
    no_levels = orderbook.get("no", []) or []
    if not yes_levels and not no_levels:
        return 0.5

    def depth_near_mid(levels: list, cents_window: float = 5.0) -> float:
        if not levels:
            return 0.0
        prices = [float(l[0]) if isinstance(l, (list, tuple)) else 0.0 for l in levels]
        mid_approx = (max(prices) + min(prices)) / 2.0 if prices else 50.0
        total = 0.0
        for level in levels:
            try:
                price, size = float(level[0]), float(level[1])
            except (IndexError, TypeError, ValueError):
                continue
            if abs(price - mid_approx) <= cents_window:
                total += size
        return total

    yes_depth = depth_near_mid(yes_levels)
    no_depth = depth_near_mid(no_levels)
    total = yes_depth + no_depth
    if total <= 0:
        return 0.5
    skew = yes_depth / total
    return abs(skew - 0.5) * 2.0
