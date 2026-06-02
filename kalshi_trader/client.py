from __future__ import annotations
import asyncio
from typing import Any
import requests
from kalshi_auth import KalshiClient as _SyncKalshiClient


class KalshiClient:
    """Async-compatible wrapper around kalshi_auth.KalshiClient.

    Uses asyncio.to_thread so blocking requests calls don't stall the event loop.
    Credentials and environment (demo/prod) are read from .env automatically.
    """

    def __init__(self):
        self._sync = _SyncKalshiClient.from_env()

    async def get(self, endpoint: str, params: dict | None = None) -> dict:
        return await asyncio.to_thread(self._sync.get, endpoint, params)

    async def post(self, endpoint: str, body: dict) -> dict:
        path = "/trade-api/v2" + endpoint
        headers = self._sync._headers("POST", path)
        resp = await asyncio.to_thread(
            requests.post,
            self._sync.base_url + endpoint,
            headers=headers,
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    async def delete(self, endpoint: str, params: dict | None = None) -> dict:
        path = "/trade-api/v2" + endpoint
        headers = self._sync._headers("DELETE", path)
        resp = await asyncio.to_thread(
            requests.delete,
            self._sync.base_url + endpoint,
            headers=headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_markets(self, status: str = "open", cursor: str = "",
                          limit: int = 200, **kwargs) -> dict:
        params: dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        params.update(kwargs)
        return await self.get("/markets", params=params)

    async def get_market(self, ticker: str) -> dict:
        return await self.get(f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str) -> dict:
        return await self.get(f"/markets/{ticker}/orderbook")

    async def get_balance(self) -> dict:
        return await self.get("/portfolio/balance")

    async def get_positions(self) -> dict:
        return await self.get("/portfolio/positions")

    async def get_fills(self, ticker: str | None = None) -> dict:
        params = {"ticker": ticker} if ticker else {}
        return await self.get("/portfolio/fills", params=params or None)

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

    async def get_trades(self, ticker: str, limit: int = 100) -> dict:
        return await self.get(f"/markets/{ticker}/trades", {"limit": limit})
