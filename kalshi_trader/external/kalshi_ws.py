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

_RECONNECT_DELAY = 5   # seconds between reconnect attempts
_WATCHDOG_TIMEOUT = 60  # force reconnect if no message received in this many seconds


def _to_cents(raw) -> int:
    """Convert a price value to integer cents.

    Kalshi's live WS feed sends prices as dollar strings like "0.3000" (= 30¢).
    Tests and legacy callers send integer or string cent values like 30 or "30".
    Heuristic: float(raw) <= 1.0 → dollar format → multiply by 100.
    """
    v = float(raw)
    return round(v * 100) if v <= 1.0 else int(v)


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
                        # Use wait_for on each receive so the watchdog fires even when
                        # the connection is silently dead (e.g. Zscaler proxy drops it
                        # while responding to pings on our behalf).
                        while self._running:
                            try:
                                msg = await asyncio.wait_for(
                                    ws.receive(), timeout=_WATCHDOG_TIMEOUT
                                )
                            except asyncio.TimeoutError:
                                log.warning("WS watchdog timeout — forcing reconnect")
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

    def _handle(self, envelope: dict) -> None:
        # Kalshi WS wraps payload in envelope["msg"] (dict); top-level has "type".
        # Guard: "msg" on error messages is a plain string, not a dict — fall back to envelope.
        msg_type = envelope.get("type", "")
        raw_inner = envelope.get("msg")
        inner = raw_inner if isinstance(raw_inner, dict) else envelope

        ticker = inner.get("market_ticker", "")

        if msg_type == "orderbook_snapshot":
            # Live feed uses "yes_dollars_fp" / "no_dollars_fp"; tests/fallback use "yes"/"no"
            yes_raw = inner.get("yes_dollars_fp", inner.get("yes", []))
            no_raw = inner.get("no_dollars_fp", inner.get("no", []))
            self.state.apply_snapshot(ticker, yes_raw, no_raw)

        elif msg_type == "orderbook_delta":
            side = inner.get("side", "yes")
            raw_price = inner.get("price", 0)
            price = _to_cents(raw_price)
            raw_delta = inner.get("delta", 0)
            delta = float(raw_delta) if isinstance(raw_delta, str) else float(raw_delta)
            self.state.apply_delta(ticker, side, price, delta)

        elif msg_type == "trade":
            raw = inner.get("count", inner.get("size", 0))
            size = float(raw) if isinstance(raw, str) else float(raw)
            self.state.record_trade(ticker, size)

        elif msg_type == "subscribed":
            log.info("WS subscription confirmed for %d tickers", len(self.tickers))
        elif msg_type == "error":
            log.error("WS error message from server: %s", envelope)
        # Unknown types are silently ignored

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()
