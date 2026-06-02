"""A read-only facade over KalshiClient.

This dashboard runs against the **prod (real-money) account**. The web layer must
never be able to place or cancel an order. This facade is the structural guarantee:
it exposes only GET-backed read methods and deliberately defines no
``post`` / ``delete`` / ``create_order`` / ``cancel_order``. The wrapped client is
private, and ``__getattr__`` raises loudly on any attempt to reach a mutating
method — so no request handler can move money even by accident or reflection.
"""
from __future__ import annotations

from typing import Any

from kalshi_trader.client import KalshiClient

# The complete set of methods the dashboard and the scoring pipeline are allowed
# to call. Everything here is a signed GET against the Kalshi API.
_ALLOWED_READ_METHODS: frozenset[str] = frozenset({
    "get",
    "get_balance",
    "get_positions",
    "get_orders",
    "get_fills",
    "get_markets",
    "get_market",
    "get_orderbook",
    "get_trades",
    "get_market_candlesticks_batch",
    "get_events",
    "get_series",
})


class ReadOnlyKalshiClient:
    """Wraps a KalshiClient and exposes only its read methods.

    Pass an instance anywhere a KalshiClient is expected (MarketScanner,
    SnapshotStore.refresh_stale): those code paths only ever call read methods
    plus the generic ``get()``, so they work through this facade unchanged.
    """

    def __init__(self, client: KalshiClient | None = None):
        # Stored under a name that won't be confused for a public API surface.
        object.__setattr__(self, "_wrapped_client", client or KalshiClient())

    # Each read method is an explicit passthrough so the allowlist is visible and
    # the facade fails closed: anything not listed here falls through to
    # __getattr__, which raises.
    async def get(self, endpoint: str, params: dict | None = None) -> dict:
        return await self._wrapped_client.get(endpoint, params)

    async def get_balance(self) -> dict:
        return await self._wrapped_client.get_balance()

    async def get_positions(self) -> dict:
        return await self._wrapped_client.get_positions()

    async def get_orders(self, status: str = "resting") -> dict:
        return await self._wrapped_client.get_orders(status=status)

    async def get_fills(self, ticker: str | None = None) -> dict:
        return await self._wrapped_client.get_fills(ticker)

    async def get_markets(self, status: str = "open", cursor: str = "",
                          limit: int = 1000, **kwargs) -> dict:
        return await self._wrapped_client.get_markets(status=status, cursor=cursor, limit=limit, **kwargs)

    async def get_market(self, ticker: str) -> dict:
        return await self._wrapped_client.get_market(ticker)

    async def get_orderbook(self, ticker: str) -> dict:
        return await self._wrapped_client.get_orderbook(ticker)

    async def get_trades(self, ticker: str, min_ts: int | None = None, limit: int = 1000) -> dict:
        return await self._wrapped_client.get_trades(ticker, min_ts=min_ts, limit=limit)

    async def get_market_candlesticks_batch(self, tickers: list[str], start_ts: int,
                                            end_ts: int, period_interval: int) -> dict:
        return await self._wrapped_client.get_market_candlesticks_batch(
            tickers, start_ts, end_ts, period_interval
        )

    async def get_events(self, status: str = "open", cursor: str = "", limit: int = 200, **kwargs) -> dict:
        return await self._wrapped_client.get_events(status=status, cursor=cursor, limit=limit, **kwargs)

    async def get_series(self) -> list[dict]:
        return await self._wrapped_client.get_series()

    def __getattr__(self, attribute_name: str) -> Any:
        # Reached only for attributes not defined above. Fail closed and shout
        # extra loudly for anything that looks like it could mutate the account.
        if (
            attribute_name in ("post", "delete", "create_order", "cancel_order")
            or attribute_name.startswith(("post", "delete", "create_", "cancel_"))
        ):
            raise AttributeError(
                f"ReadOnlyKalshiClient forbids '{attribute_name}': the dashboard is "
                "read-only and must never place or cancel orders."
            )
        raise AttributeError(
            f"ReadOnlyKalshiClient does not expose '{attribute_name}' "
            f"(allowed: {sorted(_ALLOWED_READ_METHODS)})."
        )

    def __setattr__(self, attribute_name: str, value: Any) -> None:
        raise AttributeError("ReadOnlyKalshiClient is immutable.")
