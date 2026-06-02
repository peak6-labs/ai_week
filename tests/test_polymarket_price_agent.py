from kalshi_trader.agents.polymarket_price_agent import PolymarketPriceAgent
from kalshi_trader.models import SignalEstimate


def test_parse_estimates_valid():
    agent = PolymarketPriceAgent.__new__(PolymarketPriceAgent)
    raw = '''```json
[
  {
    "source": "polymarket_price",
    "probability": 0.45,
    "uncertainty": 0.03,
    "weight": 0.75,
    "data_issued_at": "2026-06-02T12:00:00+00:00",
    "metadata": {"ticker": "NBA-CELTICS", "gap_cents": 18.0, "match_score": 0.91,
                 "data_quality": "fresh", "narrative": "test"}
  }
]
```'''
    results = agent._parse_estimates(raw)
    assert len(results) == 1
    assert isinstance(results[0], SignalEstimate)
    assert results[0].source == "polymarket_price"
    assert results[0].probability == 0.45


def test_parse_estimates_empty():
    agent = PolymarketPriceAgent.__new__(PolymarketPriceAgent)
    assert agent._parse_estimates("```json\n[]\n```") == []


def test_parse_estimates_no_block():
    agent = PolymarketPriceAgent.__new__(PolymarketPriceAgent)
    assert agent._parse_estimates("nothing useful") == []
