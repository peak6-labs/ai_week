"""KalshiClient must support `async with` so the dashboard account poller works.

The dashboard's _poll_kalshi_account uses `async with KalshiClient() as client`.
Before this was supported the poller crashed on startup with
"object does not support the asynchronous context manager protocol", leaving the
UI balance/positions stuck at zero.
"""
from __future__ import annotations

import asyncio

from kalshi_trader.client import KalshiClient


def test_supports_async_context_manager() -> None:
    async def use_it() -> KalshiClient:
        async with KalshiClient() as client:
            assert isinstance(client, KalshiClient)
            return client

    client = asyncio.run(use_it())
    assert client is not None


def test_aclose_is_idempotent() -> None:
    async def run() -> None:
        client = KalshiClient()
        await client.aclose()
        await client.aclose()  # second close must not raise

    asyncio.run(run())
