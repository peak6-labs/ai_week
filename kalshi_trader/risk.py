from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
from kalshi_trader import config
from kalshi_trader.models import TradeIdea, PortfolioState, RiskDecision


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
            ct = close_time if close_time.tzinfo else close_time.replace(tzinfo=timezone.utc)
            hours_to_close = (ct - now).total_seconds() / 3600
            if hours_to_close < config.MIN_HOURS_BEFORE_SETTLEMENT:
                return RiskDecision(False, 0, f"settlement too soon ({hours_to_close:.1f}h)")

        # Minimum edge: agent confidence must exceed market price by at least 5 cents
        market_prob = idea.market_price / 100.0
        edge = idea.confidence - market_prob
        if edge < 0.05:
            return RiskDecision(False, 0, f"insufficient edge: {edge:.3f} < 0.05")

        # Half-Kelly sizing
        size = self._half_kelly_size(
            idea.confidence, market_prob, portfolio.balance_dollars, idea.category
        )

        # Clamp within per-category and total headroom
        size = min(size, config.MAX_PER_CATEGORY_EXPOSURE_DOLLARS - cat_exposure)
        size = min(size, config.MAX_TOTAL_EXPOSURE_DOLLARS - portfolio.total_exposure_dollars)
        size = min(size, config.MAX_SINGLE_POSITION_DOLLARS)
        size = max(size, 0.0)

        if size < config.MIN_SINGLE_POSITION_DOLLARS:
            return RiskDecision(False, 0, f"sized position too small (${size:.2f}) after limits")

        fees = self.estimate_fee_dollars(idea.market_price, size)
        return RiskDecision(True, round(size, 2), fees_estimate_cents=fees * 100)

    def estimate_fee_dollars(self, price_cents: float, size_dollars: float) -> float:
        c = price_cents / 100.0
        if c <= 0:
            return 0.0
        contracts = size_dollars / c
        fee_per_contract = 0.07 * c * (1.0 - c)
        return fee_per_contract * contracts

    def record_loss(self, category: str) -> None:
        self._consecutive_losses[category] += 1
        self._consecutive_wins[category] = 0

    def record_win(self, category: str) -> None:
        self._consecutive_wins[category] += 1
        if self._consecutive_wins[category] >= 3:
            self._consecutive_losses[category] = 0

    def _half_kelly_size(
        self, p: float, market_prob: float, balance: float, category: str
    ) -> float:
        q = 1.0 - p
        b = (1.0 - market_prob) / market_prob  # net odds on YES
        if b <= 0:
            return 0.0
        f_star = (p * b - q) / b
        f_half = max(f_star / 2.0, 0.0)
        if self._consecutive_losses.get(category, 0) >= 3:
            f_half *= 0.5
        return f_half * balance
