from __future__ import annotations

from datetime import datetime, timezone

from kalshi_trader.models import Candle, Market


def volume_oi_ratio_score(market: Market) -> float:
    """Daily turnover rate: volume_24h / open_interest, capped at 50% = 1.0."""
    if market.open_interest <= 0:
        return 0.0
    ratio = market.volume_24h / market.open_interest
    return min(1.0, ratio / 0.50)


def spread_penalty_multiplier(market: Market) -> float:
    """Liquidity multiplier from the YES bid/ask spread.

    Tight spreads preserve the raw actionability score. Wider or one-sided books
    reduce the ranking score because they are harder to enter or exit cleanly.
    """
    if market.yes_bid <= 0 or market.yes_ask <= 0:
        return 0.50
    spread_cents = max(0.0, market.yes_ask - market.yes_bid)
    if spread_cents <= 2.0:
        return 1.00
    if spread_cents <= 5.0:
        return 0.95
    if spread_cents <= 10.0:
        return 0.85
    if spread_cents <= 20.0:
        return 0.70
    return 0.55


def settlement_proximity_multiplier(market: Market) -> float:
    """Time-to-settlement multiplier — favors markets that settle sooner.

    A market resolving soon is preferred over one that settles a long time from
    now: capital turns over faster and the thesis is exposed to fewer unforeseen
    developments before resolution. Applied to the composite score alongside the
    spread penalty, so a far-dated market must be meaningfully more actionable to
    outrank a near one. This is a soft down-rank only — it never zeroes a market
    out. A timezone-naive ``close_time`` is treated as UTC.
    """
    close_time = market.close_time
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=timezone.utc)
    hours_to_close = max(
        0.0, (close_time - datetime.now(timezone.utc)).total_seconds() / 3600.0
    )
    if hours_to_close <= 24.0:      # within a day
        return 1.00
    if hours_to_close <= 72.0:      # within 3 days
        return 0.90
    if hours_to_close <= 168.0:     # within a week
        return 0.78
    if hours_to_close <= 720.0:     # within 30 days
        return 0.60
    if hours_to_close <= 2160.0:    # within 90 days
        return 0.42
    return 0.28                     # beyond 90 days


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
    """Is the latest active hour unusually active vs prior active hours?

    Kalshi returns sparse candles for thin markets, so missing hours generally
    mean no activity rather than a failed cache read.

    Requires at least 2 hourly candles. Score = 0.0 at baseline, about 0.5 at
    2.5× baseline, and 1.0 at 5× baseline. Sparse 2-3 candle histories are
    capped slightly because the baseline is thinner.
    """
    if len(hourly_candles) < 2:
        return None
    sorted_candles = sorted(hourly_candles, key=lambda c: c.end_period_ts)
    last = sorted_candles[-1]
    prior = sorted_candles[:-1]
    baseline_volumes = [candle.volume for candle in prior if candle.volume >= 0]
    if not baseline_volumes:
        return None

    if last.volume < 10:
        return 0.0

    baseline = sum(baseline_volumes) / len(baseline_volumes)
    if baseline <= 0:
        score = 1.0
    else:
        ratio = last.volume / baseline
        if ratio <= 1.0:
            score = 0.0
        elif ratio <= 2.5:
            score = (ratio - 1.0) / 3.0
        else:
            score = min(1.0, 0.5 + ((ratio - 2.5) / 5.0))

    if last.volume < 25:
        score = min(score, 0.4)
    if len(hourly_candles) < 4:
        score = min(score, 0.85)
    return score


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


def live_top_of_book(orderbook: dict) -> tuple[float | None, float | None]:
    """Derive (yes_bid, yes_ask) in cents from a live orderbook.

    The book holds resting bids per side. Best YES bid = highest YES level. Best
    YES ask = 100 - highest NO bid (buying YES is selling NO). Returns None for a
    side with no resting levels.
    """
    def best_price(levels: list) -> float | None:
        prices = []
        for level in levels or []:
            try:
                prices.append(float(level[0]))
            except (IndexError, TypeError, ValueError):
                continue
        return max(prices) if prices else None

    yes_bid = best_price(orderbook.get("yes", []) if isinstance(orderbook, dict) else [])
    best_no_bid = best_price(orderbook.get("no", []) if isinstance(orderbook, dict) else [])
    yes_ask = (100.0 - best_no_bid) if best_no_bid is not None else None
    return yes_bid, yes_ask


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
