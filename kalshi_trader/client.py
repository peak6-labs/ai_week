from __future__ import annotations
import asyncio
import concurrent.futures
import functools
from typing import Any
import requests
from kalshi_auth import KalshiClient as _SyncKalshiClient
from kalshi_trader.schema import normalize_market, normalize_orderbook

# Dedicated pool so candle batch requests aren't capped at the process default
# (~22 threads on this machine). Tune against Kalshi rate limits if needed.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=64, thread_name_prefix="kalshi-http"
)


class KalshiClient:
    """Async-compatible wrapper around kalshi_auth.KalshiClient.

    Uses asyncio.to_thread so blocking requests calls don't stall the event loop.
    Credentials and environment (demo/prod) are read from .env automatically.
    """

    def __init__(self, executor: concurrent.futures.ThreadPoolExecutor | None = None):
        self._sync = _SyncKalshiClient.from_env()
        # Defaults to the shared pool; callers can pass a dedicated pool to
        # isolate latency-sensitive traffic (e.g. the dashboard's live polls)
        # from bursty bulk traffic (e.g. the candle/scoring scan).
        self._executor = executor or _EXECUTOR

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exception_details: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the per-instance requests session off the event loop.

        Each instance owns its own ``_SyncKalshiClient`` session, so closing it
        never affects the shared thread-pool executor or other clients.
        """
        session = getattr(self._sync, "_session", None)
        if session is not None:
            await asyncio.to_thread(session.close)

    async def get(self, endpoint: str, params: dict | None = None) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._sync.get, endpoint, params)

    async def post(self, endpoint: str, body: dict) -> dict:
        path = "/trade-api/v2" + endpoint
        headers = self._sync._headers("POST", path)
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            self._executor,
            functools.partial(
                self._sync._session.post,
                self._sync.base_url + endpoint,
                headers=headers,
                json=body,
                timeout=15,
            ),
        )
        resp.raise_for_status()
        return resp.json()

    async def delete(self, endpoint: str, params: dict | None = None) -> dict:
        path = "/trade-api/v2" + endpoint
        headers = self._sync._headers("DELETE", path)
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            self._executor,
            functools.partial(
                self._sync._session.delete,
                self._sync.base_url + endpoint,
                headers=headers,
                params=params,
                timeout=15,
            ),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_series(self) -> list[dict]:
        """Return all series objects (ticker + category)."""
        resp = await self.get("/series")
        return resp.get("series") or []

    async def get_series_detail(self, series_ticker: str) -> dict:
        """Return one series object, including settlement terms.

        Unlike ``get_series`` (which lists every series), this hits the
        per-series endpoint. The response carries ``settlement_sources``,
        ``contract_terms_url``, and ``contract_url`` — the fields the signal
        pipeline needs to know how a market actually settles. Mirrors
        ``get_market``: a thin wrapper over the GET so callers get the raw dict.
        """
        return await self.get(f"/series/{series_ticker}")

    async def get_events(self, status: str = "open", cursor: str = "",
                         limit: int = 200, **kwargs) -> dict:
        params: dict[str, Any] = {"status": status, "limit": limit,
                                   "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        params.update(kwargs)
        return await self.get("/events", params=params)

    async def get_markets(self, status: str = "open", cursor: str = "",
                          limit: int = 1000, **kwargs) -> dict:
        params: dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        params.update(kwargs)
        response = await self.get("/markets", params=params)
        for market in response.get("markets") or []:
            normalize_market(market)
        return response

    async def get_market(self, ticker: str) -> dict:
        response = await self.get(f"/markets/{ticker}")
        normalize_market(response.get("market", response))
        return response

    async def get_orderbook(self, ticker: str) -> dict:
        return normalize_orderbook(await self.get(f"/markets/{ticker}/orderbook"))

    async def get_balance(self) -> dict:
        return await self.get("/portfolio/balance")

    async def get_positions(self) -> dict:
        return await self.get("/portfolio/positions")

    async def get_fills(self, ticker: str | None = None) -> dict:
        params = {"ticker": ticker} if ticker else {}
        return await self.get("/portfolio/fills", params=params or None)

    async def get_orders(self, status: str = "resting") -> dict:
        """Read the account's orders (default: resting/open orders). Read-only."""
        return await self.get("/portfolio/orders", params={"status": status})

    async def create_order(self, ticker: str, action: str, side: str,
                           count: int, order_type: str = "market",
                           yes_price: int | None = None) -> dict:
        body: dict[str, Any] = {
            "ticker": ticker, "action": action, "side": side,
            "count": count, "type": order_type,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        return await self.post("/portfolio/orders", body)

    async def cancel_order(self, order_id: str) -> dict:
        return await self.delete(f"/portfolio/orders/{order_id}")

    async def get_market_candlesticks_batch(
        self,
        tickers: list[str],
        start_ts: int,
        end_ts: int,
        period_interval: int,
    ) -> dict:
        """Fetch OHLCV candles for up to 100 tickers in one request.

        period_interval: minutes — 1, 60, or 1440.
        """
        params: dict[str, Any] = {
            "market_tickers": ",".join(tickers),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
        return await self.get("/markets/candlesticks", params=params)

    async def get_trades(
        self,
        ticker: str,
        min_ts: int | None = None,
        limit: int = 1000,
    ) -> dict:
        """Fetch recent public trades for a market."""
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if min_ts is not None:
            params["min_ts"] = min_ts
        return await self.get("/markets/trades", params=params)
