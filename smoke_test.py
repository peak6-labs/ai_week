"""Minimal live API smoke test — 3 requests total.

Fetches:
1. 3 markets from Gamma API
2. Trades for the first market's conditionId
3. Positions for the first wallet found in trades

Prints raw field names so we can verify our assumptions.
"""
import asyncio
import json
import ssl
import aiohttp
import truststore


def _ssl_context():
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


async def main():
    connector = aiohttp.TCPConnector(ssl=_ssl_context())
    async with aiohttp.ClientSession(connector=connector) as session:

        # --- 1. Gamma: fetch 3 markets ---
        print("=== GET /markets (limit=3) ===")
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": "3"},
        ) as resp:
            markets = await resp.json()
        print(f"Status: {resp.status}  Count: {len(markets)}")
        if markets:
            print("First market keys:", list(markets[0].keys()))
            print("Sample:")
            for k in ("id", "question", "conditionId", "outcomePrices", "volume24hr", "active", "closed", "updatedAt"):
                print(f"  {k!r}: {markets[0].get(k, '<MISSING>')}")

        condition_id = markets[0].get("conditionId") if markets else None
        if not condition_id:
            print("No conditionId found — stopping.")
            return

        # --- 2. Data API: fetch trades for first market ---
        print(f"\n=== GET /trades (market={condition_id}, limit=5) ===")
        async with session.get(
            "https://data-api.polymarket.com/trades",
            params={"market": condition_id, "limit": "5"},
        ) as resp:
            trades = await resp.json()
        print(f"Status: {resp.status}  Count: {len(trades)}")
        if trades:
            print("First trade keys:", list(trades[0].keys()))
            print("Sample:")
            for k in ("proxyWallet", "side", "size", "price", "timestamp",
                      "conditionId", "title", "outcome", "transactionHash"):
                print(f"  {k!r}: {trades[0].get(k, '<MISSING>')}")

        wallet = trades[0].get("proxyWallet") if trades else None
        if not wallet:
            print("No proxyWallet found — stopping.")
            return

        # --- 3. Data API: fetch positions for first wallet ---
        print(f"\n=== GET /positions (user={wallet[:10]}…, limit=3) ===")
        async with session.get(
            "https://data-api.polymarket.com/positions",
            params={"user": wallet, "limit": "3"},
        ) as resp:
            positions = await resp.json()
        print(f"Status: {resp.status}  Count: {len(positions)}")
        if positions:
            print("First position keys:", list(positions[0].keys()))
            print("Sample:")
            for k in ("proxyWallet", "conditionId", "title", "size",
                      "avgPrice", "curPrice", "cashPnl", "percentPnl", "outcome"):
                print(f"  {k!r}: {positions[0].get(k, '<MISSING>')}")


asyncio.run(main())
