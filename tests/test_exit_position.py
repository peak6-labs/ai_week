"""Tests for scripts/exit_position.py.

The script places a single limit sell order. Tests mock KalshiClient.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import kalshi_trader.config  # noqa: F401 — loads .env

exit_position_module = importlib.import_module("scripts.exit_position")


def _make_client(order_id: str = "ord_abc", status: str = "resting") -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.create_order = AsyncMock(
        return_value={"order": {"order_id": order_id, "status": status}}
    )
    return client


@pytest.mark.asyncio
async def test_exit_position_places_limit_sell():
    """Places a limit sell order with correct parameters."""
    client = _make_client()
    with patch("scripts.exit_position.KalshiClient", return_value=client):
        result = await exit_position_module.exit_position(
            ticker="KXTEST-1", side="yes", quantity=10, yes_price=38
        )
    assert result["order_id"] == "ord_abc"
    assert result["order_status"] == "resting"
    assert result["dry_run"] is False
    client.create_order.assert_awaited_once_with(
        ticker="KXTEST-1",
        action="sell",
        side="yes",
        count=10,
        order_type="limit",
        yes_price=38,
    )


@pytest.mark.asyncio
async def test_exit_position_dry_run_does_not_call_api():
    """dry_run=True prints intent but places no order."""
    client = _make_client()
    with patch("scripts.exit_position.KalshiClient", return_value=client):
        result = await exit_position_module.exit_position(
            ticker="KXTEST-1", side="no", quantity=5, yes_price=62, dry_run=True
        )
    assert result["dry_run"] is True
    assert result["order_id"] is None
    assert result["order_status"] is None
    client.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_position_returns_correct_fields():
    """Result dict contains ticker, side, quantity, yes_price, order_id."""
    client = _make_client(order_id="ord_xyz")
    with patch("scripts.exit_position.KalshiClient", return_value=client):
        result = await exit_position_module.exit_position(
            ticker="KXTEST-2", side="yes", quantity=3, yes_price=75
        )
    assert result["ticker"] == "KXTEST-2"
    assert result["side"] == "yes"
    assert result["quantity"] == 3
    assert result["yes_price"] == 75
    assert result["order_id"] == "ord_xyz"


@pytest.mark.asyncio
async def test_exit_position_no_side_correct_params():
    """NO-side order passes side='no' to create_order."""
    client = _make_client()
    with patch("scripts.exit_position.KalshiClient", return_value=client):
        await exit_position_module.exit_position(
            ticker="KXTEST-3", side="no", quantity=8, yes_price=30
        )
    client.create_order.assert_awaited_once_with(
        ticker="KXTEST-3",
        action="sell",
        side="no",
        count=8,
        order_type="limit",
        yes_price=30,
    )
