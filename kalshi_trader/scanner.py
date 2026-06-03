from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_trader._retry import with_retry
from kalshi_trader.models import Market, ScoredMarket
from kalshi_trader.actionability import MarketScorer, SnapshotStore
from kalshi_trader.market_snapshot import load_snapshot

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

# Ticker prefixes for US-listed equity markets. Blocked unconditionally —
# even when a --category override is passed — because equity markets require
# separate regulatory treatment.
BLOCKED_EQUITY_PREFIXES: tuple[str, ...] = (
    "KXINX",            # S&P 500 intraday/daily
    "KXNASDAQ",         # Nasdaq 100 intraday/daily/positional
    "KXNDQ",            # Nasdaq short-form (e.g. KXNDQDIRY)
    "KXEARNINGSM",      # Earnings-call mention markets (individual stocks)
    "KXSNAP",           # Snap Inc.
    "KXCMG",            # Chipotle Mexican Grill
    "KXPM-",            # Philip Morris
    "KXBA-",            # Boeing
    "KXSTOCKBAN",       # Stock ban/delist markets
)


def filter_markets(
    markets: list[Market],
    category: str | None,
    now_dt: datetime,
) -> list[Market]:
    """Apply the standard pipeline filters (close_time, category, OI/vol)."""
    markets = [market for market in markets if market.close_time > now_dt]
    if category:
        markets = [market for market in markets if market.category == category.lower()]
    else:
        markets = [market for market in markets if market.category in SCORED_CATEGORIES]
    markets = [
        market for market in markets
        if not market.ticker.startswith(BLOCKED_EQUITY_PREFIXES)
    ]
    markets = [market for market in markets if market.open_interest >= 100 and market.volume_24h >= 10]
    return markets


class MarketScanner:
    def __init__(self, client):
        self._client = client

    async def get_open_markets(
        self,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[Market]:
        markets = []
        cursor = ""
        page = 0
        while True:
            resp = await self._client.get_markets(status="open", cursor=cursor, limit=1000)
            page += 1
            batch = resp.get("markets", [])
            for market_data in batch:
                markets.append(self._parse_market(market_data))
            cursor = resp.get("cursor", "")
            _log.info("Page %d: %d this page, %d total so far%s",
                      page, len(batch), len(markets),
                      "" if cursor else " — last page")
            if not cursor:
                break
            if limit is not None and len(markets) >= limit:
                markets = markets[:limit]
                _log.info("Reached --limit %d, stopping early", limit)
                break
        if category:
            markets = [market for market in markets if market.category == category]
        return markets

    async def get_market_with_orderbook(self, ticker: str) -> tuple[Market, dict]:
        market_response = await self._client.get_market(ticker)
        orderbook_response = await self._client.get_orderbook(ticker)
        return self._parse_market(market_response["market"]), orderbook_response.get("orderbook", {})

    async def enrich_categories(self, markets: list[Market]) -> None:
        """Fetch category for each market from the events endpoint and set it in-place.

        The /markets endpoint returns empty category fields in prod; the /events
        endpoint has the correct category. Fetches unique event_tickers in parallel
        and normalises to lowercase to match SCORED_CATEGORIES.
        """
        event_tickers = list({market.event_ticker for market in markets if market.event_ticker})
        if not event_tickers:
            return

        _log.info("Fetching categories for %d unique events...", len(event_tickers))
        concurrency_semaphore = asyncio.Semaphore(20)

        async def _fetch(event_ticker: str) -> tuple[str, str]:
            async with concurrency_semaphore:
                try:
                    resp = await self._client.get(f"/events/{event_ticker}", {})
                    category = resp.get("event", {}).get("category", "") or ""
                    return event_ticker, category.lower()
                except Exception:
                    return event_ticker, "unknown"

        pairs = await asyncio.gather(*[_fetch(event_ticker) for event_ticker in event_tickers])
        category_map = dict(pairs)

        for market in markets:
            category = category_map.get(market.event_ticker, "")
            if category:
                market.category = category

        _log.info("Category enrichment complete")

    async def get_scored_markets(
        self,
        scorer: MarketScorer,
        store: SnapshotStore,
        trade_top_n: int = 50,
        orderbook_top_n: int = 40,
        category: str | None = None,
        markets_file: Path | str | None = None,
        should_enrich_categories: bool = False,
        markets: list[Market] | None = None,
    ) -> list[ScoredMarket]:
        """Score open markets and return them ranked by actionability.

        Flow:
          1. Use a pre-supplied ``markets`` list, else load from ``markets_file``,
             else fetch all open markets (live)
          2. (live only, opt-in) Enrich categories from /events so the category
             filter works — prod /markets returns empty category fields
          3. Filter to active categories + markets with open_interest >= 100 and volume_24h >= 10
          4. Refresh stale candles for filtered tickers (SQLite cache; API only when stale)
          5. Compute candle-based signals for all markets
          6. Fetch live trades for top N → OFI signal
          7. Fetch live orderbooks for top N → depth skew signal
          8. Re-rank and return

        ``markets`` lets a caller supply an already-loaded, already-filtered list
        (the dashboard does this from a cached snapshot — the prod live universe is
        ~half a million markets, far too large to paginate + enrich every cycle).
        ``should_enrich_categories`` defaults False to preserve the CLI's existing
        behavior. Snapshot loads (markets_file) are already category-enriched at fetch.
        """
        now = int(time.time())
        now_dt = datetime.now(timezone.utc)

        if markets is not None:
            _log.info("Scoring %d pre-supplied (pre-filtered) markets", len(markets))
        elif markets_file is not None:
            _log.info("Loading markets from snapshot: %s", markets_file)
            snapshot_markets = load_snapshot(markets_file, now_dt)
            _log.info("Loaded %d non-expired markets from snapshot", len(snapshot_markets))
            markets = filter_markets(snapshot_markets, category, now_dt)
        else:
            _log.info("Fetching all open markets...")
            markets = await self.get_open_markets()
            _log.info("Found %d open markets", len(markets))
            if should_enrich_categories:
                await self.enrich_categories(markets)
            markets = filter_markets(markets, category, now_dt)
            _log.info("After filters: %d markets", len(markets))

        _log.info("Scoring %d active markets after filters", len(markets))
        all_tickers = [market.ticker for market in markets]

        _log.info("Checking candle cache for %d tickers...", len(all_tickers))
        await store.refresh_stale(all_tickers, self._client, now)

        _log.info("Scoring all markets from cache...")
        scored = scorer.score_all(markets, store)

        top_trade_tickers = [scored_market.market.ticker for scored_market in scored[:trade_top_n]]
        top_ob_tickers = [scored_market.market.ticker for scored_market in scored[:orderbook_top_n]]

        _log.info(
            "Fetching live trades for top %d markets and orderbooks for top %d...",
            len(top_trade_tickers), len(top_ob_tickers),
        )
        live_semaphore = asyncio.Semaphore(10)

        async def _get_trades(ticker):
            async with live_semaphore:
                return await with_retry(self._client.get_trades, ticker, min_ts=now - 7200)

        async def _get_orderbook(ticker):
            async with live_semaphore:
                return await with_retry(self._client.get_orderbook, ticker)

        trade_responses, orderbook_responses = await asyncio.gather(
            asyncio.gather(*[_get_trades(ticker) for ticker in top_trade_tickers]),
            asyncio.gather(*[_get_orderbook(ticker) for ticker in top_ob_tickers]),
        )

        trade_data = {
            ticker: (response.get("trades") or [])
            for ticker, response in zip(top_trade_tickers, trade_responses)
        }
        orderbook_data = {
            ticker: (response.get("orderbook") or {})
            for ticker, response in zip(top_ob_tickers, orderbook_responses)
        }

        _log.info("Enriching with live signals and finalising ranking...")
        result = scorer.enrich_with_live(scored, trade_data, orderbook_data)
        if result:
            _log.info("Scoring complete — top market: %s (%.3f)", result[0].market.ticker, result[0].composite_score)
        else:
            _log.warning("Scoring complete — no markets passed filters")
        return result

    def _parse_market(self, market_data: dict) -> Market:
        raw_close = market_data.get("close_time", "")
        if isinstance(raw_close, str) and raw_close:
            try:
                close_datetime = datetime.fromisoformat(raw_close.replace("Z", "+00:00"))
            except ValueError:
                close_datetime = datetime.now(timezone.utc)
        else:
            close_datetime = datetime.now(timezone.utc)

        # /markets API uses cents integers; /events nested markets use dollar floats.
        def _cents(key_cents: str, key_dollars: str) -> float:
            if market_data.get(key_cents) is not None:
                return float(market_data[key_cents])
            return float(market_data.get(key_dollars) or 0) * 100

        return Market(
            ticker=market_data.get("ticker", ""),
            event_ticker=market_data.get("event_ticker", ""),
            series_ticker=market_data.get("series_ticker", ""),
            title=market_data.get("title", ""),
            yes_bid=_cents("yes_bid", "yes_bid_dollars"),
            yes_ask=_cents("yes_ask", "yes_ask_dollars"),
            last_price=_cents("last_price", "last_price_dollars"),
            volume_24h=int(float(market_data.get("volume") or market_data.get("volume_24h_fp") or 0)),
            open_interest=int(float(market_data.get("open_interest") or market_data.get("open_interest_fp") or 0)),
            category=(market_data.get("category") or market_data.get("series_ticker") or "unknown").lower(),
            close_time=close_datetime,
            status=market_data.get("status", "open"),
        )
