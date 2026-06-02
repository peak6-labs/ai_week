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
    volumes = [candle.volume for candle in daily_candles if candle.volume > 0]
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
    baseline_volumes = [candle.volume for candle in prior if candle.volume >= 0]
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
    open_interest_now = sorted_candles[-1].open_interest
    open_interest_old = sorted_candles[0].open_interest
    if open_interest_old <= 0:
        return None
    open_interest_delta_ratio = (open_interest_now - open_interest_old) / open_interest_old
    return min(1.0, max(0.0, open_interest_delta_ratio / 0.10))


def momentum_score(hourly_candles: list[Candle]) -> float | None:
    """Is price moving with conviction over the last 4 hours?

    Uses trade price (price_close), skipping candles with no trades.
    10-cent move over 4h = full score.
    Returns None if fewer than 4 hourly candles available.
    """
    if len(hourly_candles) < 4:
        return None
    sorted_candles = sorted(hourly_candles, key=lambda c: c.end_period_ts)
    recent_candles = sorted_candles[-4:]
    close_prices = [candle.price_close for candle in recent_candles if candle.price_close is not None]
    if len(close_prices) < 2:
        return 0.0
    price_delta = abs(close_prices[-1] - close_prices[0])
    return min(1.0, price_delta / 10.0)


def hl_position_score(candles: list[Candle], current_price: float) -> float | None:
    """Is price at an extreme of its range over the given candles?

    Score = 0 at midrange, 1 at high or low.
    Returns None if no candles; 0.5 if the range is flat.
    """
    if not candles:
        return None
    close_prices = [candle.price_close for candle in candles if candle.price_close is not None]
    close_prices.append(current_price)
    if len(close_prices) < 2:
        return None
    price_low = min(close_prices)
    price_high = max(close_prices)
    if price_high - price_low < 2.0:
        return None
    normalized_position = (current_price - price_low) / (price_high - price_low)
    return abs(normalized_position - 0.5) * 2.0


def ofi_score(trades: list[dict]) -> float | None:
    """Are trades directionally one-sided? (Order Flow Imbalance)

    Uses taker_outcome_side: "yes" = buyer-initiated, "no" = seller-initiated.
    Score = 0.0 when balanced, 1.0 when fully one-sided.
    Returns None if no trades.
    """
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
    total_volume = yes_volume + no_volume
    if total_volume <= 0:
        return None
    return abs(yes_volume - no_volume) / total_volume


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
        prices = [float(level_entry[0]) if isinstance(level_entry, (list, tuple)) else 0.0 for level_entry in levels]
        midpoint_approximation = (max(prices) + min(prices)) / 2.0 if prices else 50.0
        total_depth = 0.0
        for level in levels:
            try:
                price, size = float(level[0]), float(level[1])
            except (IndexError, TypeError, ValueError):
                continue
            if abs(price - midpoint_approximation) <= cents_window:
                total_depth += size
        return total_depth

    yes_depth = depth_near_mid(yes_levels)
    no_depth = depth_near_mid(no_levels)
    total_depth = yes_depth + no_depth
    if total_depth <= 0:
        return 0.5
    yes_side_skew = yes_depth / total_depth
    return abs(yes_side_skew - 0.5) * 2.0
