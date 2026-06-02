"""Integration test — requires XAI_API_KEY in environment. Skipped in CI."""
import os
import pytest
from kalshi_trader.external.x_client import XClient


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("XAI_API_KEY"),
    reason="XAI_API_KEY not set — integration test skipped",
)
async def test_live_search_returns_valid_structure():
    client = XClient()
    try:
        result = await client.live_search(
            "Celtics win NBA championship prediction",
            "Will the Celtics win the 2026 NBA championship?",
        )
        assert 0.0 <= result["probability"] <= 1.0
        assert 0.0 <= result["uncertainty"] <= 1.0
        assert isinstance(result["summary"], str)
        assert isinstance(result["key_quotes"], list)
        assert isinstance(result["velocity"], dict)
        assert "1h" in result["velocity"]
        assert isinstance(result["issued_at"], str)
    finally:
        await client.close()
