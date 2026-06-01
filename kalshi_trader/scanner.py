from __future__ import annotations
from datetime import datetime
from kalshi_trader.models import Market


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
                                                   limit=200, **kwargs)
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
