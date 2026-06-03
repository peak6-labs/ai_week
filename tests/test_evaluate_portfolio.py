"""Tests for scripts/evaluate_portfolio.py.

The script is async; tests mock KalshiClient to avoid real API calls.
Import the module directly after inserting the project root on sys.path.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import kalshi_trader.config  # noqa: F401 — loads .env

evaluate_portfolio = importlib.import_module("scripts.evaluate_portfolio")


def _make_client(positions: list[dict], markets: list[dict], order_id: str = "ord_test") -> MagicMock:
    """Build a mock KalshiClient configured with the given API responses."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get_positions = AsyncMock(return_value={"market_positions": positions})
    client.get_markets = AsyncMock(return_value={"markets": markets})
    client.create_order = AsyncMock(return_value={"order": {"order_id": order_id, "status": "resting"}})
    return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_position(ticker: str, position_fp: str, market_exposure_dollars: str) -> dict:
    return {
        "ticker": ticker,
        "position_fp": position_fp,
        "market_exposure_dollars": market_exposure_dollars,
        "realized_pnl_dollars": "0",
        "fees_paid_dollars": "0",
    }


def _market_prices(ticker: str, yes_bid: float, yes_ask: float) -> dict:
    return {"ticker": ticker, "yes_bid": yes_bid, "yes_ask": yes_ask, "last_price": (yes_bid + yes_ask) / 2}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_loss_position_exits_live():
    """A YES position down 30% triggers a sell limit order."""
    # 10 contracts, cost=$5 => avg=50c. Current midpoint=35c => value=$3.50 < $3.75
    raw = _raw_position("KXTEST-1", "10.00", "5.00")
    market = _market_prices("KXTEST-1", yes_bid=33.0, yes_ask=37.0)  # midpoint=35c
    client = _make_client([raw], [market])

    with patch("scripts.evaluate_portfolio.KalshiClient", return_value=client):
        results = await evaluate_portfolio.run(dry_run=False, out=None)

    assert results["triggered_count"] == 1
    exit_record = results["exits"][0]
    assert exit_record["ticker"] == "KXTEST-1"
    assert exit_record["reason"] == "stop_loss"
    assert exit_record["exit_price_cents"] == 35   # round((33+37)/2)
    assert exit_record["order_id"] == "ord_test"
    client.create_order.assert_awaited_once_with(
        ticker="KXTEST-1", action="sell", side="yes",
        count=10, order_type="limit", yes_price=35,
    )


@pytest.mark.asyncio
async def test_profit_target_position_exits_live():
    """A YES position up 80% triggers a sell limit order."""
    # 10 contracts, cost=$5 => avg=50c. Current midpoint=90c => value=$9.00 > $8.75
    raw = _raw_position("KXTEST-2", "10.00", "5.00")
    market = _market_prices("KXTEST-2", yes_bid=88.0, yes_ask=92.0)  # midpoint=90c
    client = _make_client([raw], [market])

    with patch("scripts.evaluate_portfolio.KalshiClient", return_value=client):
        results = await evaluate_portfolio.run(dry_run=False, out=None)

    assert results["triggered_count"] == 1
    assert results["exits"][0]["reason"] == "profit_target"


@pytest.mark.asyncio
async def test_clean_position_is_not_exited():
    """A position within bounds produces no sell order and appears in clean_positions."""
    # 10 contracts, cost=$5. Midpoint=50c => value=$5.00 — no trigger.
    raw = _raw_position("KXTEST-3", "10.00", "5.00")
    market = _market_prices("KXTEST-3", yes_bid=48.0, yes_ask=52.0)  # midpoint=50c
    client = _make_client([raw], [market])

    with patch("scripts.evaluate_portfolio.KalshiClient", return_value=client):
        results = await evaluate_portfolio.run(dry_run=False, out=None)

    assert results["triggered_count"] == 0
    assert len(results["clean_positions"]) == 1
    client.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_does_not_call_create_order():
    """dry_run=True computes triggers but never places orders."""
    raw = _raw_position("KXTEST-4", "10.00", "5.00")
    market = _market_prices("KXTEST-4", yes_bid=33.0, yes_ask=37.0)  # midpoint=35c, stop-loss
    client = _make_client([raw], [market])

    with patch("scripts.evaluate_portfolio.KalshiClient", return_value=client):
        results = await evaluate_portfolio.run(dry_run=True, out=None)

    assert results["triggered_count"] == 1
    assert results["exits"][0]["order_id"] is None
    client.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_live_price_goes_to_errors_not_crash():
    """A position whose ticker has no live price is logged as error, not an exception."""
    raw = _raw_position("KXTEST-5", "10.00", "5.00")
    # get_markets returns empty list — no prices
    client = _make_client([raw], [])

    with patch("scripts.evaluate_portfolio.KalshiClient", return_value=client):
        results = await evaluate_portfolio.run(dry_run=False, out=None)

    assert results["triggered_count"] == 0
    assert len(results["errors"]) == 1
    assert results["errors"][0]["ticker"] == "KXTEST-5"


@pytest.mark.asyncio
async def test_no_open_positions_returns_empty_results():
    """No open positions → no API calls beyond get_positions."""
    client = _make_client([], [])

    with patch("scripts.evaluate_portfolio.KalshiClient", return_value=client):
        results = await evaluate_portfolio.run(dry_run=False, out=None)

    assert results["total_positions"] == 0
    assert results["triggered_count"] == 0
    client.get_markets.assert_not_awaited()


@pytest.mark.asyncio
async def test_out_writes_json_file(tmp_path):
    """--out path writes valid JSON results file."""
    raw = _raw_position("KXTEST-6", "10.00", "5.00")
    market = _market_prices("KXTEST-6", yes_bid=48.0, yes_ask=52.0)
    client = _make_client([raw], [market])
    out_path = str(tmp_path / "results.json")

    with patch("scripts.evaluate_portfolio.KalshiClient", return_value=client):
        await evaluate_portfolio.run(dry_run=False, out=out_path)

    data = json.loads(Path(out_path).read_text())
    assert "evaluated_at" in data
    assert "exits" in data
