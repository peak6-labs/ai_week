from __future__ import annotations
import asyncio
from dataclasses import dataclass
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_trader._retry import with_retry
from kalshi_trader.actionability.signals import live_top_of_book
from kalshi_trader.models import Market, ScanMetadata, ScanResult, ScoredMarket
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


@dataclass(frozen=True)
class ScannerConfig:
    live_price_batch_size: int = 100
    shortlist_max_live_price_age_seconds: int = 60
    missing_live_price_policy: str = "drop"


def _normalize_categories(
    category: str | None = None,
    categories: list[str] | tuple[str, ...] | set[str] | None = None,
) -> set[str] | None:
    if category and categories:
        raise ValueError("Pass either category or categories, not both")
    if category:
        return {category.lower()}
    if categories:
        normalized = {value.lower() for value in categories if value}
        return normalized or None
    return None


def filter_markets(
    markets: list[Market],
    category: str | None = None,
    now_dt: datetime | None = None,
    categories: list[str] | tuple[str, ...] | set[str] | None = None,
) -> list[Market]:
    """Apply the standard pipeline filters (close_time, category, OI/vol)."""
    category_filter = _normalize_categories(category=category, categories=categories)
    effective_now = now_dt or datetime.now(timezone.utc)
    markets = [market for market in markets if market.close_time > effective_now]
    if category_filter:
        markets = [market for market in markets if market.category in category_filter]
    else:
        markets = [market for market in markets if market.category in SCORED_CATEGORIES]
    markets = [
        market for market in markets
        if not market.ticker.startswith(BLOCKED_EQUITY_PREFIXES)
    ]
    markets = [market for market in markets if market.open_interest >= 100 and market.volume_24h >= 10]
    return markets


class MarketScanner:
    def __init__(self, client, config: ScannerConfig | None = None):
        self._client = client
        self._config = config or ScannerConfig()

    async def get_open_markets(
        self,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[Market]:
        categories = _normalize_categories(category=category)
        markets = []
        cursor = ""
        page = 0
        while True:
            response = await with_retry(
                self._client.get_markets, status="open", cursor=cursor, limit=1000
            )
            page += 1
            batch = response.get("markets", [])
            for market_data in batch:
                markets.append(self._parse_market(market_data))
            cursor = response.get("cursor", "")
            _log.info("Page %d: %d this page, %d total so far%s",
                      page, len(batch), len(markets),
                      "" if cursor else " — last page")
            if not cursor:
                break
            if limit is not None and len(markets) >= limit:
                markets = markets[:limit]
                _log.info("Reached --limit %d, stopping early", limit)
                break
        if categories:
            markets = [market for market in markets if market.category in categories]
        return markets

    async def get_market_with_orderbook(self, ticker: str) -> tuple[Market, dict]:
        market_response = await self._client.get_market(ticker)
        orderbook_response = await self._client.get_orderbook(ticker)
        return self._parse_market(market_response["market"]), orderbook_response.get("orderbook", {})

    async def enrich_categories(self, markets: list[Market]) -> None:
        """Set category on each market using a single bulk /series fetch.

        The /markets endpoint returns empty category fields in prod. Rather than
        making one /events/{ticker} call per unique event (potentially 100k+
        requests), we fetch all series in one call and map series_ticker →
        category. Each market's series_ticker is already populated by
        _parse_market.
        """
        _log.info("Fetching categories via /series (single bulk call)...")
        try:
            series_list = await with_retry(self._client.get_series)
        except Exception as caught_exception:
            _log.warning("Series fetch failed, categories will be empty: %s", caught_exception)
            return

        series_category_map: dict[str, str] = {
            s["ticker"]: (s.get("category") or "").lower()
            for s in series_list
            if s.get("ticker")
        }
        _log.info("Series fetch complete: %d series loaded", len(series_category_map))

        enriched = 0
        for market in markets:
            series_ticker = market.series_ticker or market.ticker.split("-")[0]
            category = series_category_map.get(series_ticker, "")
            if category:
                market.category = category
                enriched += 1

        _log.info("Category enrichment complete: %d/%d markets enriched", enriched, len(markets))

    async def _fetch_live_prices(self, tickers: list[str]) -> tuple[dict[str, dict[str, float | None]], list[str]]:
        quotes: dict[str, dict[str, float | None]] = {}
        failed_tickers: list[str] = []
        for start in range(0, len(tickers), self._config.live_price_batch_size):
            chunk = tickers[start:start + self._config.live_price_batch_size]
            try:
                response = await self._client.get_markets(
                    status="open",
                    tickers=",".join(chunk),
                    limit=len(chunk),
                )
            except Exception as caught_exception:
                _log.warning("Live price batch failed for %d tickers: %s", len(chunk), caught_exception)
                failed_tickers.extend(chunk)
                continue
            for market_data in response.get("markets") or []:
                ticker = market_data.get("ticker")
                if ticker:
                    quotes[ticker] = {
                        "yes_bid": market_data.get("yes_bid"),
                        "yes_ask": market_data.get("yes_ask"),
                        "last_price": market_data.get("last_price"),
                    }
        return quotes, failed_tickers

    def _apply_live_prices(
        self,
        markets: list[Market],
        quotes: dict[str, dict[str, float | None]],
        metadata: ScanMetadata,
    ) -> list[Market]:
        live_markets: list[Market] = []
        dropped_tickers: list[str] = []
        missing_price_policy = self._config.missing_live_price_policy.lower()
        for market in markets:
            quote = quotes.get(market.ticker)
            if quote is None:
                dropped_tickers.append(market.ticker)
                continue
            yes_bid = quote.get("yes_bid")
            yes_ask = quote.get("yes_ask")
            last_price = quote.get("last_price")
            if yes_bid is None or yes_ask is None or last_price is None:
                dropped_tickers.append(market.ticker)
                continue
            market.yes_bid = float(yes_bid)
            market.yes_ask = float(yes_ask)
            market.last_price = float(last_price)
            live_markets.append(market)
        metadata.live_priced_ticker_count = len(live_markets)
        metadata.dropped_unquoted_ticker_count = len(dropped_tickers)
        if dropped_tickers:
            message = (
                f"Live pricing incomplete: quoted {len(live_markets)}/{len(markets)} filtered tickers; "
                f"{'dropped' if missing_price_policy == 'drop' else 'excluded'} "
                f"{len(dropped_tickers)} unquoted/missing tickers"
            )
            metadata.degraded = True
            metadata.degraded_reason = message
            _log.warning(message)
        return live_markets

    def _mark_degraded(self, metadata: ScanMetadata, reason: str) -> None:
        metadata.degraded = True
        metadata.degraded_reason = reason
        _log.warning(reason)

    def _refresh_shortlist_prices(
        self,
        scored: list[ScoredMarket],
        shortlisted_tickers: list[str],
        quotes: dict[str, dict[str, float | None]],
        orderbook_data: dict[str, dict],
    ) -> None:
        by_ticker = {scored_market.market.ticker: scored_market for scored_market in scored}
        for ticker in shortlisted_tickers:
            scored_market = by_ticker.get(ticker)
            if scored_market is None:
                continue
            quote = quotes.get(ticker) or {}
            last_price = quote.get("last_price")
            if last_price is not None:
                scored_market.market.last_price = float(last_price)
            yes_bid = quote.get("yes_bid")
            yes_ask = quote.get("yes_ask")
            if yes_bid is not None:
                scored_market.market.yes_bid = float(yes_bid)
            if yes_ask is not None:
                scored_market.market.yes_ask = float(yes_ask)
            orderbook = orderbook_data.get(ticker)
            if orderbook:
                live_yes_bid, live_yes_ask = live_top_of_book(orderbook)
                if live_yes_bid is not None:
                    scored_market.market.yes_bid = live_yes_bid
                if live_yes_ask is not None:
                    scored_market.market.yes_ask = live_yes_ask

    async def run_scan(
        self,
        scorer: MarketScorer,
        store: SnapshotStore,
        trade_top_n: int = 50,
        orderbook_top_n: int = 40,
        category: str | None = None,
        categories: list[str] | tuple[str, ...] | set[str] | None = None,
        markets_file: Path | str | None = None,
        should_enrich_categories: bool = False,
        markets: list[Market] | None = None,
    ) -> ScanResult:
        """Score markets via snapshot/live hybrid pricing and return scan metadata."""
        normalized_categories = _normalize_categories(category=category, categories=categories)
        metadata = ScanMetadata()
        now = int(time.time())
        now_dt = datetime.now(timezone.utc)

        if markets is not None:
            _log.info("Scoring %d pre-supplied catalog markets", len(markets))
        elif markets_file is not None:
            _log.info("Loading markets from snapshot: %s", markets_file)
            markets = load_snapshot(markets_file, now_dt)
            _log.info("Loaded %d non-expired markets from snapshot", len(markets))
        else:
            _log.info("Fetching all open markets...")
            markets = await self.get_open_markets()
            _log.info("Found %d open markets", len(markets))
            if should_enrich_categories:
                await self.enrich_categories(markets)

        markets = filter_markets(markets or [], now_dt=now_dt, categories=normalized_categories)
        metadata.filtered_ticker_count = len(markets)
        _log.info("After filters: %d markets", len(markets))
        if not markets:
            return ScanResult(ranked_markets=[], metadata=metadata)

        quotes, failed_tickers = await self._fetch_live_prices([market.ticker for market in markets])
        metadata.live_prices_refreshed_at = datetime.now(timezone.utc)
        if failed_tickers:
            self._mark_degraded(
                metadata,
                f"Live pricing batch failed for {len(failed_tickers)} filtered tickers",
            )

        live_markets = self._apply_live_prices(markets, quotes, metadata)
        if not live_markets:
            if not metadata.degraded_reason:
                self._mark_degraded(metadata, "Live pricing returned no quoted markets for scoring")
            return ScanResult(ranked_markets=[], metadata=metadata)

        all_tickers = [market.ticker for market in live_markets]
        _log.info("Checking candle cache for %d tickers...", len(all_tickers))
        await store.refresh_stale(all_tickers, self._client, now)

        _log.info("Scoring all live-priced markets from cache...")
        scored = scorer.score_all(live_markets, store)

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

        shortlist_size = max(trade_top_n, orderbook_top_n)
        shortlist_tickers = [scored_market.market.ticker for scored_market in result[:shortlist_size]]
        if shortlist_tickers:
            shortlist_quotes, shortlist_failed = await self._fetch_live_prices(shortlist_tickers)
            metadata.shortlist_refreshed_at = datetime.now(timezone.utc)
            if shortlist_failed:
                self._mark_degraded(
                    metadata,
                    f"Shortlist live refresh failed for {len(shortlist_failed)} tickers",
                )
            missing_shortlist_quotes = [
                ticker for ticker in shortlist_tickers
                if ticker not in shortlist_quotes and not orderbook_data.get(ticker)
            ]
            if missing_shortlist_quotes:
                self._mark_degraded(
                    metadata,
                    f"Shortlist live refresh returned no quote for {len(missing_shortlist_quotes)} tickers",
                )
            self._refresh_shortlist_prices(result, shortlist_tickers, shortlist_quotes, orderbook_data)
            scorer.rescore(result)
            refresh_age = (datetime.now(timezone.utc) - metadata.shortlist_refreshed_at).total_seconds()
            if refresh_age > self._config.shortlist_max_live_price_age_seconds:
                self._mark_degraded(
                    metadata,
                    f"Shortlist live prices exceeded freshness budget ({refresh_age:.1f}s)",
                )

        if result:
            _log.info("Scoring complete — top market: %s (%.3f)", result[0].market.ticker, result[0].composite_score)
        else:
            _log.warning("Scoring complete — no markets passed filters")
        return ScanResult(ranked_markets=result, metadata=metadata)

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
        categories: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> list[ScoredMarket]:
        return (
            await self.run_scan(
                scorer=scorer,
                store=store,
                trade_top_n=trade_top_n,
                orderbook_top_n=orderbook_top_n,
                category=category,
                categories=categories,
                markets_file=markets_file,
                should_enrich_categories=should_enrich_categories,
                markets=markets,
            )
        ).ranked_markets

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
