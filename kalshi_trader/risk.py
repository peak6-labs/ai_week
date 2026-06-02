from __future__ import annotations
import math
from collections import defaultdict
from datetime import datetime, timezone
from kalshi_trader import config
from kalshi_trader.models import TradeIdea, PortfolioState, RiskDecision


# Kalshi trading-fee coefficients. The fee scales with contracts * price * (1 - price)
# and is rounded up to the next whole cent per order. See plans/trading_plan.md
# section 2 ("Kalshi Fee Structure") for the authoritative description.
#   - General markets: takers pay GENERAL_TAKER_FEE_COEFFICIENT; makers are free.
#   - Index markets (S&P 500 / Nasdaq): both makers and takers pay the reduced
#     INDEX_MARKET_FEE_COEFFICIENT.
# These rates are per-market and change over time; the `fee` fields on the API's
# /markets responses are the source of truth before relying on a specific number.
GENERAL_TAKER_FEE_COEFFICIENT = 0.07
INDEX_MARKET_FEE_COEFFICIENT = 0.035


class RiskManager:
    def __init__(self):
        self._consecutive_losses: dict[str, int] = defaultdict(int)
        self._consecutive_wins: dict[str, int] = defaultdict(int)

    def check_trade(
        self,
        idea: TradeIdea,
        portfolio: PortfolioState,
        close_time: datetime | None = None,
    ) -> RiskDecision:
        # Hard: daily loss limit
        if portfolio.daily_realized_pnl <= -config.DAILY_LOSS_LIMIT_DOLLARS:
            return RiskDecision(False, 0, "daily loss limit reached — system paused")

        # Hard: total exposure
        if portfolio.total_exposure_dollars >= config.MAX_TOTAL_EXPOSURE_DOLLARS:
            return RiskDecision(False, 0, "max total exposure reached ($400)")

        # Hard: category exposure
        cat_exposure = portfolio.exposure_by_category.get(idea.category, 0.0)
        if cat_exposure >= config.MAX_PER_CATEGORY_EXPOSURE_DOLLARS:
            return RiskDecision(False, 0, f"category exposure limit reached for {idea.category}")

        # Hard: settlement proximity
        if close_time is not None:
            now = datetime.now(timezone.utc)
            # Normalise naive close_time to UTC-aware for comparison
            utc_close_time = close_time if close_time.tzinfo else close_time.replace(tzinfo=timezone.utc)
            hours_to_close = (utc_close_time - now).total_seconds() / 3600
            if hours_to_close < config.MIN_HOURS_BEFORE_SETTLEMENT:
                return RiskDecision(False, 0, f"settlement too soon ({hours_to_close:.1f}h)")

        # Minimum edge: agent confidence must exceed market price by at least 5 cents
        market_prob = idea.market_price / 100.0
        edge = idea.confidence - market_prob
        if edge < 0.05:
            return RiskDecision(False, 0, f"insufficient edge: {edge:.3f} < 0.05")

        # Half-Kelly sizing
        size = self._half_kelly_size(
            probability=idea.confidence, market_prob=market_prob,
            balance=portfolio.balance_dollars, category=idea.category,
        )

        # Clamp within per-category and total headroom
        size = min(size, config.MAX_PER_CATEGORY_EXPOSURE_DOLLARS - cat_exposure)
        size = min(size, config.MAX_TOTAL_EXPOSURE_DOLLARS - portfolio.total_exposure_dollars)
        size = min(size, config.MAX_SINGLE_POSITION_DOLLARS)
        size = max(size, 0.0)

        if size < config.MIN_SINGLE_POSITION_DOLLARS:
            return RiskDecision(False, 0, f"sized position too small (${size:.2f}) after limits")

        # Trade ideas enter by crossing the spread, so assume the taker rate on a
        # general market (index markets are filtered out of the tradeable universe).
        fees = self.estimate_fee_dollars(idea.market_price, size)
        return RiskDecision(True, round(size, 2), fees_estimate_cents=fees * 100)

    def estimate_fee_dollars(
        self,
        price_cents: float,
        size_dollars: float,
        is_maker: bool = False,
        is_index_market: bool = False,
    ) -> float:
        price_in_dollars = price_cents / 100.0
        if price_in_dollars <= 0.0 or price_in_dollars >= 1.0 or size_dollars <= 0.0:
            return 0.0
        coefficient = self._fee_coefficient(is_maker=is_maker, is_index_market=is_index_market)
        if coefficient <= 0.0:
            return 0.0
        contracts = size_dollars / price_in_dollars
        raw_fee_cents = coefficient * contracts * price_in_dollars * (1.0 - price_in_dollars) * 100.0
        # Kalshi rounds each order's fee up to the next whole cent. Round away
        # floating-point noise first so a fee landing exactly on a cent boundary
        # (e.g. $2.10 -> 210.00000000000003) isn't pushed to the next cent.
        return math.ceil(round(raw_fee_cents, 6)) / 100.0

    def _fee_coefficient(self, is_maker: bool, is_index_market: bool) -> float:
        if is_index_market:
            # Index markets (S&P 500 / Nasdaq) charge the reduced rate on both sides.
            return INDEX_MARKET_FEE_COEFFICIENT
        # General markets: takers pay the standard rate; makers are free.
        return 0.0 if is_maker else GENERAL_TAKER_FEE_COEFFICIENT

    def record_loss(self, category: str) -> None:
        self._consecutive_losses[category] += 1
        self._consecutive_wins[category] = 0

    def record_win(self, category: str) -> None:
        self._consecutive_wins[category] += 1
        if self._consecutive_wins[category] >= 3:
            self._consecutive_losses[category] = 0

    def _half_kelly_size(
        self, probability: float, market_prob: float, balance: float, category: str
    ) -> float:
        complement_probability = 1.0 - probability
        yes_net_odds = (1.0 - market_prob) / market_prob  # net odds on YES
        if yes_net_odds <= 0:
            return 0.0
        full_kelly_fraction = (probability * yes_net_odds - complement_probability) / yes_net_odds
        half_kelly_fraction = max(full_kelly_fraction / 2.0, 0.0)
        if self._consecutive_losses.get(category, 0) >= 3:
            half_kelly_fraction *= 0.5
        return half_kelly_fraction * balance
