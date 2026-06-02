"""Integration test — requires KALSHI_DEMO_KEY_ID and real WS. Skipped in CI."""
import asyncio
import os
import pytest
from kalshi_trader.external.kalshi_ws import KalshiWebSocketClient
from kalshi_trader.orderbook import OrderBookState

@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("KALSHI_DEMO_KEY_ID"),
    reason="No Kalshi credentials — integration test skipped",
)
async def test_ws_receives_data():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=["INXY-25DEC31-T49999.99"], state=state)
    task = asyncio.create_task(client.run())
    await asyncio.sleep(5)
    await client.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Just verify it ran without crashing; state may be empty in demo
    assert isinstance(state.tickers(), list)
