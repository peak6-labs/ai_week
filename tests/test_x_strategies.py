from datetime import datetime
import pytest
from unittest.mock import AsyncMock
from kalshi_trader.external.x_strategies import (
    SentimentStrategy, NewsDetectionStrategy, ExpertOpinionStrategy, BuzzStrategy,
    CATEGORY_STRATEGIES, STRATEGY_NAME_MAP, FALLBACK_STRATEGIES,
)
from kalshi_trader.external.x_client import GrokSearchResult


def _make_grok_result(probability: float = 0.65, uncertainty: float = 0.1) -> GrokSearchResult:
    return GrokSearchResult(
        probability=probability, uncertainty=uncertainty,
        summary="Test summary.", key_quotes=["q1"],
        sentiment_breakdown={"positive": 0.6, "negative": 0.2, "neutral": 0.2},
        source_quality={"high_follower": 0.4, "general": 0.6},
        velocity={"1h": 5, "6h": 20, "24h": 80},
        key_entities=["Entity1"], contrarian_signal="",
        issued_at="2026-06-01T12:00:00",
    )


def test_sentiment_query_contains_market_title():
    q = SentimentStrategy().build_query("Will Celtics win the championship?")
    assert "Celtics win the championship" in q


def test_news_query_contains_market_title():
    q = NewsDetectionStrategy().build_query("Will inflation exceed 3%?")
    assert "inflation" in q.lower() or "exceed 3%" in q


def test_expert_query_contains_market_title():
    q = ExpertOpinionStrategy().build_query("Will it rain in NYC tomorrow?")
    assert "NYC" in q or "rain" in q.lower()


def test_buzz_query_contains_market_title():
    q = BuzzStrategy().build_query("Will Lakers win tonight?")
    assert "Lakers" in q


def test_all_source_tags_are_unique():
    tags = [cls().source_tag for cls in [SentimentStrategy, NewsDetectionStrategy, ExpertOpinionStrategy, BuzzStrategy]]
    assert len(tags) == len(set(tags))


def test_all_source_tags_start_with_x_grok():
    for cls in [SentimentStrategy, NewsDetectionStrategy, ExpertOpinionStrategy, BuzzStrategy]:
        assert cls().source_tag.startswith("x_grok_")


def test_category_strategies_has_required_categories():
    for cat in ("weather", "mentions", "politics", "sports"):
        assert cat in CATEGORY_STRATEGIES
        assert len(CATEGORY_STRATEGIES[cat]) >= 1


def test_strategy_name_map_has_all_four_names():
    for name in ("sentiment", "news", "experts", "buzz"):
        assert name in STRATEGY_NAME_MAP


def test_fallback_strategies_is_nonempty():
    assert len(FALLBACK_STRATEGIES) >= 1


@pytest.mark.asyncio
async def test_to_signal_estimate_maps_fields():
    strategy = SentimentStrategy()
    result = _make_grok_result(probability=0.72, uncertainty=0.08)
    estimate = strategy.to_signal_estimate(result)
    assert estimate.source == "x_grok_sentiment"
    assert estimate.probability == 0.72
    assert estimate.uncertainty == 0.08
    assert estimate.metadata["summary"] == "Test summary."
    assert "key_quotes" in estimate.metadata
    assert "velocity" in estimate.metadata


@pytest.mark.asyncio
async def test_run_delegates_to_client():
    strategy = NewsDetectionStrategy()
    mock_client = AsyncMock()
    mock_client.live_search = AsyncMock(return_value=_make_grok_result())
    await strategy.run("Will there be a recession?", mock_client)
    mock_client.live_search.assert_called_once()
    call_args = mock_client.live_search.call_args
    assert "recession" in call_args[0][0].lower() or "recession" in str(call_args)
