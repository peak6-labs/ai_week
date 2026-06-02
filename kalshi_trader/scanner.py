from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone
from kalshi_trader.models import Market, ScoredMarket
from kalshi_trader.actionability import MarketScorer, SnapshotStore, orderbook_skew_score

_log = logging.getLogger(__name__)

# Categories scored by default (when --category is not specified).
# Markets outside this set are skipped before candle refresh.
SCORED_CATEGORIES: frozenset[str] = frozenset({
    "elections",
    "politics",
    "entertainment",
    "climate and weather",
    "mentions",
    "economics",
    "science and technology",
})


class MarketScanner:
    def __init__(self, client):
        self._client = client

    async def get_open_markets(self, category: str | None = None) -> list[Market]:
        markets = []
        cursor = ""
        while True:
            kwargs: dict = {}
            if category:
                kwargs["series_ticker"] = category
            resp = await self._client.get_markets(status="open", cursor=cursor,
                                                   limit=1000, **kwargs)
            for m in resp.get("markets", []):
                markets.append(self._parse_market(m))
            cursor = resp.get("cursor", "")
            if not cursor:
                break
        if category:
            markets = [m for m in markets if m.category == category]
        return markets

    async def get_market_with_orderbook(self, ticker: str) -> tuple[Market, dict]:
        market_resp = await self._client.get_market(ticker)
        ob_resp = await self._client.get_orderbook(ticker)
        return self._parse_market(market_resp["market"]), ob_resp.get("orderbook", {})

    async def get_scored_markets(
        self,
        scorer: MarketScorer,
        store: SnapshotStore,
        trade_top_n: int = 50,
        orderbook_top_n: int = 20,
        category: str | None = None,
    ) -> list[ScoredMarket]:
        """Score open markets and return them ranked by actionability.

        Flow:
          1. Fetch all open markets (live)
          2. Filter to active categories + markets with open_interest > 0 and volume_24h > 0
          3. Refresh stale candles for filtered tickers (SQLite cache; API only when stale)
          4. Compute candle-based signals for all markets
          5. Fetch live trades for top N → OFI signal
          6. Fetch live orderbooks for top N → depth skew signal
          7. Re-rank and return
        """
        now = int(time.time())

        _log.info("Fetching all open markets...")
        markets = await self.get_open_markets()
        _log.info("Found %d open markets", len(markets))

        now_dt = datetime.now(timezone.utc)

        # Drop markets that are no longer open for trading.
        markets = [m for m in markets if m.status == "open" and m.close_time > now_dt]

        # Filter to active categories before the expensive candle refresh.
        if category:
            markets = [m for m in markets if m.category == category]
        else:
            markets = [m for m in markets if m.category in SCORED_CATEGORIES]

        # Drop illiquid markets below meaningful activity thresholds.
        markets = [m for m in markets if m.open_interest >= 100 and m.volume_24h >= 10]

        _log.info("Scoring %d active markets after filters", len(markets))
        all_tickers = [m.ticker for m in markets]

        _log.info("Checking candle cache for %d tickers...", len(all_tickers))
        await store.refresh_stale(all_tickers, self._client, now)

        _log.info("Scoring all markets from cache...")
        scored = scorer.score_all(markets, store)

        top_trade_tickers = [s.market.ticker for s in scored[:trade_top_n]]
        top_ob_tickers = [s.market.ticker for s in scored[:orderbook_top_n]]

        _log.info(
            "Fetching live trades for top %d markets and orderbooks for top %d...",
            len(top_trade_tickers), len(top_ob_tickers),
        )
        trade_responses, ob_responses = await asyncio.gather(
            asyncio.gather(*[
                self._client.get_trades(t, min_ts=now - 7200)
                for t in top_trade_tickers
            ]),
            asyncio.gather(*[
                self._client.get_orderbook(t)
                for t in top_ob_tickers
            ]),
        )

        trade_data = {
            t: (r.get("trades") or [])
            for t, r in zip(top_trade_tickers, trade_responses)
        }
        orderbook_data = {
            t: (r.get("orderbook") or {})
            for t, r in zip(top_ob_tickers, ob_responses)
        }

        _log.info("Enriching with live signals and finalising ranking...")
        result = scorer.enrich_with_live(scored, trade_data, orderbook_data)
        _log.info("Scoring complete — top market: %s (%.3f)", result[0].market.ticker, result[0].composite_score)
        return result

    def _parse_market(self, m: dict) -> Market:
        raw_close = m.get("close_time", "")
        if isinstance(raw_close, str) and raw_close:
            try:
                dt = datetime.fromisoformat(raw_close.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.utcnow()
        else:
            dt = datetime.utcnow()

        return Market(
            ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            series_ticker=m.get("series_ticker", ""),
            title=m.get("title", ""),
            yes_bid=float(m.get("yes_bid", 0)),
            yes_ask=float(m.get("yes_ask", 0)),
            last_price=float(m.get("last_price", 0)),
            volume_24h=int(m.get("volume", 0)),
            open_interest=int(m.get("open_interest", 0)),
            category=m.get("category", m.get("series_ticker", "unknown")),
            close_time=dt,
            status=m.get("status", "open"),
        )
