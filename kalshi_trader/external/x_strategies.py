from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from kalshi_trader.models import SignalEstimate
from kalshi_trader.external.x_client import XClient, GrokSearchResult
from kalshi_trader import config


def _parse_issued_at(issued_at: str) -> datetime:
    try:
        return datetime.fromisoformat(issued_at.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc).replace(tzinfo=None)


class BaseXStrategy(ABC):
    source_tag: str

    @abstractmethod
    def build_query(self, market_title: str) -> str: ...

    async def run(self, market_title: str, client: XClient) -> GrokSearchResult:
        query = self.build_query(market_title)
        return await client.live_search(query, market_title)

    def to_signal_estimate(self, result: GrokSearchResult) -> SignalEstimate:
        return SignalEstimate(
            source=self.source_tag,
            probability=result["probability"],
            uncertainty=result["uncertainty"],
            weight=config.X_GROK_SIGNAL_WEIGHT,
            data_issued_at=_parse_issued_at(result["issued_at"]),
            metadata={
                "summary": result["summary"],
                "key_quotes": result["key_quotes"],
                "sentiment_breakdown": result["sentiment_breakdown"],
                "source_quality": result["source_quality"],
                "velocity": result["velocity"],
                "key_entities": result["key_entities"],
                "contrarian_signal": result["contrarian_signal"],
            },
        )


class SentimentStrategy(BaseXStrategy):
    source_tag = "x_grok_sentiment"

    def build_query(self, market_title: str) -> str:
        return f"{market_title} prediction odds probability sentiment"


class NewsDetectionStrategy(BaseXStrategy):
    source_tag = "x_grok_news"

    def build_query(self, market_title: str) -> str:
        return f"{market_title} breaking news latest update"


class ExpertOpinionStrategy(BaseXStrategy):
    source_tag = "x_grok_experts"

    def build_query(self, market_title: str) -> str:
        return f"{market_title} expert forecast analysis opinion"


class BuzzStrategy(BaseXStrategy):
    source_tag = "x_grok_buzz"

    def build_query(self, market_title: str) -> str:
        return market_title


CATEGORY_STRATEGIES: dict[str, list[type[BaseXStrategy]]] = {
    "weather":  [ExpertOpinionStrategy, NewsDetectionStrategy],
    "mentions": [BuzzStrategy, SentimentStrategy],
    "politics": [SentimentStrategy, NewsDetectionStrategy],
    "sports":   [NewsDetectionStrategy, SentimentStrategy],
}

STRATEGY_NAME_MAP: dict[str, type[BaseXStrategy]] = {
    "sentiment": SentimentStrategy,
    "news":      NewsDetectionStrategy,
    "experts":   ExpertOpinionStrategy,
    "buzz":      BuzzStrategy,
}

FALLBACK_STRATEGIES: list[type[BaseXStrategy]] = [BuzzStrategy, SentimentStrategy]
