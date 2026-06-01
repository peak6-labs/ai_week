import pytest
from unittest.mock import AsyncMock
from kalshi_trader.scanner import MarketScanner


@pytest.mark.asyncio
async def test_scanner_returns_markets(mock_client):
    scanner = MarketScanner(mock_client)
    markets = await scanner.get_open_markets()
    assert len(markets) > 0
    assert all(m.status == "open" for m in markets)


@pytest.mark.asyncio
async def test_scanner_filters_by_category(mock_client):
    scanner = MarketScanner(mock_client)
    markets = await scanner.get_open_markets(category="sports")
    assert all(m.category == "sports" for m in markets)


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.get_markets.return_value = {
        "markets": [
            {
                "ticker": "NBA-CELTICS-WIN", "event_ticker": "NBA-FINALS",
                "series_ticker": "NBA", "title": "Celtics win?",
                "yes_bid": 21, "yes_ask": 23, "last_price": 22,
                "volume": 50000, "open_interest": 12000,
                "category": "sports", "close_time": "2026-06-05T23:00:00Z",
                "status": "open",
            }
        ],
        "cursor": "",
    }
    return client
