from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kalshi_trader.dashboard.scoring_loop import run_one_scan_cycle
from kalshi_trader.models import Market, ScanMetadata, ScanResult, ScoredMarket
from kalshi_trader.scanner import MarketScanner


def _market(
    ticker: str = "KXELEC-PRES-2028",
    category: str = "elections",
    yes_bid: float = 45.0,
    yes_ask: float = 47.0,
    last_price: float = 46.0,
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXELEC-PRES",
        series_ticker="KXELEC",
        title="Who wins the 2028 election?",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        last_price=last_price,
        volume_24h=50000,
        open_interest=12000,
        category=category,
        close_time=datetime.now(timezone.utc) + timedelta(days=30),
        status="active",
    )


@pytest.mark.asyncio
async def test_scanner_returns_markets(mock_client):
    scanner = MarketScanner(mock_client)
    markets = await scanner.get_open_markets()
    assert len(markets) > 0
    assert all(market.status is not None for market in markets)


@pytest.mark.asyncio
async def test_scanner_filters_by_category(mock_client):
    scanner = MarketScanner(mock_client)
    markets = await scanner.get_open_markets(category="sports")
    assert all(market.category == "sports" for market in markets)


@pytest.mark.asyncio
async def test_run_scan_reprices_filtered_markets_before_scoring():
    client = AsyncMock()
    client.get_markets.return_value = {
        "markets": [
            {"ticker": "KXELEC-PRES-2028", "yes_bid": 61.0, "yes_ask": 63.0, "last_price": 62.0}
        ]
    }
    client.get_trades.return_value = {"trades": []}
    client.get_orderbook.return_value = {"orderbook": {}}

    scanner = MarketScanner(client)
    store = SimpleNamespace(refresh_stale=AsyncMock())
    market = _market(yes_bid=10.0, yes_ask=12.0, last_price=11.0)

    def _score_all(markets: list[Market], _store) -> list[ScoredMarket]:
        return [ScoredMarket(market=markets[0], composite_score=0.5, volume_oi_ratio_score=0.5)]

    scorer = SimpleNamespace(
        score_all=_score_all,
        enrich_with_live=lambda ranked, *_: ranked,
        rescore=lambda ranked: ranked,
    )

    result = await scanner.run_scan(scorer, store, markets=[market], trade_top_n=0, orderbook_top_n=0)

    assert result.ranked_markets[0].market.yes_bid == 61.0
    assert result.ranked_markets[0].market.yes_ask == 63.0
    assert result.ranked_markets[0].market.last_price == 62.0
    store.refresh_stale.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scan_drops_unquoted_markets_from_scoring():
    client = AsyncMock()
    client.get_markets.return_value = {
        "markets": [
            {"ticker": "KXGOOD", "yes_bid": 40.0, "yes_ask": 42.0, "last_price": 41.0},
            {"ticker": "KXBAD", "yes_bid": None, "yes_ask": 55.0, "last_price": 54.0},
        ]
    }
    client.get_trades.return_value = {"trades": []}
    client.get_orderbook.return_value = {"orderbook": {}}

    scanner = MarketScanner(client)
    store = SimpleNamespace(refresh_stale=AsyncMock())
    seen_tickers: list[str] = []

    def _score_all(markets: list[Market], _store) -> list[ScoredMarket]:
        seen_tickers.extend(market.ticker for market in markets)
        return [ScoredMarket(market=market, composite_score=0.5, volume_oi_ratio_score=0.5) for market in markets]

    scorer = SimpleNamespace(
        score_all=_score_all,
        enrich_with_live=lambda ranked, *_: ranked,
        rescore=lambda ranked: ranked,
    )

    result = await scanner.run_scan(
        scorer,
        store,
        markets=[_market("KXGOOD"), _market("KXBAD")],
        trade_top_n=0,
        orderbook_top_n=0,
    )

    assert seen_tickers == ["KXGOOD"]
    assert result.metadata.live_priced_ticker_count == 1
    assert result.metadata.dropped_unquoted_ticker_count == 1
    assert result.metadata.degraded is True


@pytest.mark.asyncio
async def test_run_scan_shortlist_refresh_prefers_orderbook_top_of_book():
    client = AsyncMock()
    client.get_markets.side_effect = [
        {
            "markets": [
                {"ticker": "KXTOP", "yes_bid": 20.0, "yes_ask": 24.0, "last_price": 22.0}
            ]
        },
        {
            "markets": [
                {"ticker": "KXTOP", "yes_bid": 21.0, "yes_ask": 25.0, "last_price": 23.0}
            ]
        },
    ]
    client.get_trades.return_value = {"trades": []}
    client.get_orderbook.return_value = {
        "orderbook": {
            "yes": [[33, 10]],
            "no": [[41, 8]],
        }
    }

    scanner = MarketScanner(client)
    store = SimpleNamespace(refresh_stale=AsyncMock())
    scorer = SimpleNamespace(
        score_all=lambda markets, _: [ScoredMarket(market=markets[0], composite_score=0.5, volume_oi_ratio_score=0.5)],
        enrich_with_live=lambda ranked, *_: ranked,
        rescore=lambda ranked: ranked,
    )

    result = await scanner.run_scan(
        scorer,
        store,
        markets=[_market("KXTOP")],
        trade_top_n=1,
        orderbook_top_n=1,
    )

    top_market = result.ranked_markets[0].market
    assert top_market.yes_bid == 33
    assert top_market.yes_ask == 59
    assert top_market.last_price == 23.0
    assert result.metadata.shortlist_refreshed_at is not None


@pytest.mark.asyncio
async def test_dashboard_cycle_marks_partial_live_price_scan_degraded():
    metadata = ScanMetadata(
        filtered_ticker_count=10,
        live_priced_ticker_count=8,
        dropped_unquoted_ticker_count=2,
        degraded=True,
        degraded_reason="Live pricing incomplete",
    )
    market = _market("KXTOP")
    ranked = [ScoredMarket(market=market, composite_score=0.5, volume_oi_ratio_score=0.5)]
    scanner = SimpleNamespace(run_scan=AsyncMock(return_value=ScanResult(ranked_markets=ranked, metadata=metadata)))
    state = SimpleNamespace(
        scan_in_progress=False,
        markets_file=None,
        cached_universe_markets=None,
        cached_universe_mtime=None,
        scanner=scanner,
        scorer=object(),
        snapshot_store=object(),
        scanner_categories=("elections",),
        scored_slate_grouped=None,
        scored_slate_markets={},
        scored_slate_generated_at=None,
        scored_slate_metadata=None,
        last_scan_error=None,
        scan_cycle_number=0,
    )

    await run_one_scan_cycle(state)

    assert state.last_scan_error == "Live pricing incomplete"
    assert state.scored_slate_metadata is metadata
    assert state.scan_cycle_number == 1


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.get_markets.return_value = {
        "markets": [
            {
                "ticker": "KXELEC-PRES-2028",
                "event_ticker": "KXELEC-PRES",
                "series_ticker": "KXELEC",
                "title": "Who wins the 2028 election?",
                "yes_bid": 45,
                "yes_ask": 47,
                "last_price": 46,
                "volume": 50000,
                "open_interest": 12000,
                "category": "sports",
                "close_time": "2028-11-05T23:00:00Z",
                "status": "active",
            }
        ],
        "cursor": "",
    }
    return client
