from kalshi_trader.agents.polymarket_whale_agent import PolymarketWhaleAgent
from kalshi_trader.models import SignalEstimate


def test_parse_estimates_valid():
    agent = PolymarketWhaleAgent.__new__(PolymarketWhaleAgent)
    raw = '''```json
[
  {
    "source": "polymarket_whale",
    "probability": 0.62,
    "uncertainty": 0.10,
    "weight": 0.60,
    "data_issued_at": "2026-06-02T11:30:00+00:00",
    "metadata": {"ticker": "NBA-CELTICS", "whale_count": 3, "data_quality": "fresh", "narrative": "test"}
  }
]
```'''
    results = agent._parse_estimates(raw)
    assert len(results) == 1
    assert isinstance(results[0], SignalEstimate)
    assert results[0].source == "polymarket_whale"
    assert results[0].metadata["whale_count"] == 3


def test_parse_estimates_empty():
    agent = PolymarketWhaleAgent.__new__(PolymarketWhaleAgent)
    assert agent._parse_estimates("```json\n[]\n```") == []
