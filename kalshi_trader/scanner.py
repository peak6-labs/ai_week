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
        page = 0
        while True:
            kwargs: dict = {}
            if category:
                kwargs["series_ticker"] = category
            resp = await self._client.get_markets(status="open", cursor=cursor,
                                                   limit=1000, **kwargs)
            page += 1
            batch = resp.get("markets", [])
            for m in batch:
                markets.append(self._parse_market(m))
            cursor = resp.get("cursor", "")
            _log.info("Page %d: %d this page, %d total so far%s",
                      page, len(batch), len(markets),
                      "" if cursor else " — last page")
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
        before = len(markets)

        markets = [m for m in markets if m.close_time > now_dt]
        _log.info("After close_time filter: %d/%d", len(markets), before)

        before = len(markets)
        if category:
            markets = [m for m in markets if m.category == category]
        else:
            markets = [m for m in markets if m.category in SCORED_CATEGORIES]
        _log.info("After category filter: %d/%d (sample categories: %s)",
                  len(markets), before,
                  list({m.category for m in markets})[:5])

        before = len(markets)
        markets = [m for m in markets if m.open_interest >= 100 and m.volume_24h >= 10]
        _log.info("After OI/vol filter: %d/%d", len(markets), before)

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
        live_sem = asyncio.Semaphore(10)

        async def _with_retry(coro_fn, *args, **kwargs):
            for attempt in range(4):
                try:
                    return await coro_fn(*args, **kwargs)
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status == 429:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        raise
            return {}

        async def _get_trades(ticker):
            async with live_sem:
                return await _with_retry(self._client.get_trades, ticker, min_ts=now - 7200)

        async def _get_orderbook(ticker):
            async with live_sem:
                return await _with_retry(self._client.get_orderbook, ticker)

        trade_responses, ob_responses = await asyncio.gather(
            asyncio.gather(*[_get_trades(t) for t in top_trade_tickers]),
            asyncio.gather(*[_get_orderbook(t) for t in top_ob_tickers]),
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
        if result:
            _log.info("Scoring complete — top market: %s (%.3f)", result[0].market.ticker, result[0].composite_score)
        else:
            _log.warning("Scoring complete — no markets passed filters")
        return result

    def _parse_market(self, m: dict) -> Market:
        raw_close = m.get("close_time", "")
        if isinstance(raw_close, str) and raw_close:
            try:
                dt = datetime.fromisoformat(raw_close.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now(timezone.utc)
        else:
            dt = datetime.now(timezone.utc)

        # /markets API uses cents integers; /events nested markets use dollar floats.
        def _cents(key_cents: str, key_dollars: str) -> float:
            if m.get(key_cents) is not None:
                return float(m[key_cents])
            return float(m.get(key_dollars) or 0) * 100

        return Market(
            ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            series_ticker=m.get("series_ticker", ""),
            title=m.get("title", ""),
            yes_bid=_cents("yes_bid", "yes_bid_dollars"),
            yes_ask=_cents("yes_ask", "yes_ask_dollars"),
            last_price=_cents("last_price", "last_price_dollars"),
            volume_24h=int(float(m.get("volume") or m.get("volume_24h_fp") or 0)),
            open_interest=int(float(m.get("open_interest") or m.get("open_interest_fp") or 0)),
            category=m.get("category", m.get("series_ticker", "unknown")),
            close_time=dt,
            status=m.get("status", "open"),
        )
