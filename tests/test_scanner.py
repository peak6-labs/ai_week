import pytest
from unittest.mock import AsyncMock
from kalshi_trader.scanner import MarketScanner


@pytest.mark.asyncio
async def test_scanner_returns_markets(mock_client):
    scanner = MarketScanner(mock_client)
    markets = await scanner.get_open_markets()
    assert len(markets) > 0
    assert all(m.status is not None for m in markets)


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
                "ticker": "KXELEC-PRES-2028", "event_ticker": "KXELEC-PRES",
                "series_ticker": "KXELEC", "title": "Who wins the 2028 election?",
                "yes_bid": 45, "yes_ask": 47, "last_price": 46,
                "volume": 50000, "open_interest": 12000,
                "category": "elections", "close_time": "2028-11-05T23:00:00Z",
                "status": "active",
            }
        ],
        "cursor": "",
    }
    return client
