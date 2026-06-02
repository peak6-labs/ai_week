"""Kalshi WebSocket client.

Maintains a real-time connection to the Kalshi order book feed and applies
incoming deltas/snapshots to a shared OrderBookState. Reconnects automatically
on disconnect.

Auth uses the same RSA-signed headers as the REST client — passed in the HTTP
upgrade request. If the Kalshi WS endpoint rejects headers and requires
first-message auth instead, send the auth dict as the first JSON message before
the subscribe command:

    await ws.send_str(json.dumps({
        "id": 0,
        "cmd": "auth",
        "params": {
            "key_id": headers["KALSHI-ACCESS-KEY"],
            "signature": headers["KALSHI-ACCESS-SIGNATURE"],
            "timestamp": headers["KALSHI-ACCESS-TIMESTAMP"],
        },
    }))
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from typing import Callable, Awaitable

import aiohttp
import truststore

from kalshi_trader import config
from kalshi_trader.orderbook import OrderBookState

log = logging.getLogger(__name__)

_RECONNECT_DELAY = 5  # seconds between reconnect attempts


def _ws_headers() -> dict:
    """Build RSA-signed auth headers for the WS upgrade request."""
    from kalshi_auth import KalshiClient as _Auth
    client = _Auth.from_env()
    ts = str(int(time.time() * 1000))
    path = "/trade-api/ws/v2"
    return {
        "KALSHI-ACCESS-KEY": client.key_id,
        "KALSHI-ACCESS-SIGNATURE": client._sign(ts, "GET", path),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


def _ssl_context() -> ssl.SSLContext:
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class KalshiWebSocketClient:
    def __init__(self, tickers: list[str], state: OrderBookState | None = None):
        self.tickers = tickers
        self.state = state or OrderBookState()
        self._running = False
        self._session: aiohttp.ClientSession | None = None

    async def run(self) -> None:
        """Connect and maintain the WS connection. Reconnects on drop. Call stop() to exit."""
        self._running = True
        ssl_ctx = _ssl_context()
        while self._running:
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=ssl_ctx)
                ) as session:
                    self._session = session
                    async with session.ws_connect(
                        config.KALSHI_WS_URL,
                        headers=_ws_headers(),
                        heartbeat=30,
                    ) as ws:
                        log.info("WS connected to %s", config.KALSHI_WS_URL)
                        await self._subscribe(ws)
                        async for msg in ws:
                            if not self._running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle(json.loads(msg.data))
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                log.warning("WS closed/error: %s", msg)
                                break
            except Exception as exc:
                log.warning("WS error: %s — reconnecting in %ds", exc, _RECONNECT_DELAY)
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        msg = {
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": self.tickers,
            },
        }
        await ws.send_str(json.dumps(msg))

    def _handle(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        ticker = msg.get("market_ticker", "")

        if msg_type == "orderbook_snapshot":
            yes_book = msg.get("yes", [])
            no_book = msg.get("no", [])
            self.state.apply_snapshot(ticker, yes_book, no_book)

        elif msg_type == "orderbook_delta":
            side = msg.get("side", "yes")
            price = int(msg.get("price", 0))
            delta = int(msg.get("delta", 0))
            self.state.apply_delta(ticker, side, price, delta)

        elif msg_type == "trade":
            size = int(msg.get("count", 0))
            self.state.record_trade(ticker, size)

        # "subscribed" and "error" are lifecycle messages — logged but not handled structurally
        elif msg_type == "subscribed":
            log.info("WS subscription confirmed for tickers: %s", self.tickers)
        elif msg_type == "error":
            log.error("WS error message from server: %s", msg)
        # Unknown types are silently ignored

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()
