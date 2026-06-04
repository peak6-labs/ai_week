"""Tests for scripts/place_order.py"""
from __future__ import annotations
import importlib
import json
import math
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import kalshi_trader.config  # noqa: F401

place_order = importlib.import_module("scripts.place_order")


def _ob(yes_bid: int, no_bid: int) -> dict:
    """Build a minimal normalized orderbook dict."""
    return {"orderbook": {"yes": [[yes_bid, 100]], "no": [[no_bid, 100]]}}


# --- midmarket_maker ---

def test_midmarket_maker_sell_rounds_up():
    # best_bid=62, best_ask=65 (100-35), midpoint=63.5, ceil=64
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "midmarket_maker") == 64


def test_midmarket_maker_buy_rounds_down():
    # midpoint=63.5, floor=63
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "midmarket_maker") == 63


def test_midmarket_maker_sell_even_spread_stays_maker():
    # best_bid=62, best_ask=64 (100-36), midpoint=63.0, ceil=63, max(63, 63)=63 > bid 62
    assert place_order.compute_limit_price(_ob(62, 36), "sell", "midmarket_maker") == 63


def test_midmarket_maker_sell_spread_1_falls_back_to_join_ask():
    # best_bid=62, best_ask=63 (100-37), spread=1 → join_ask=63
    assert place_order.compute_limit_price(_ob(62, 37), "sell", "midmarket_maker") == 63


def test_midmarket_maker_buy_spread_1_falls_back_to_join_bid():
    # best_bid=62, best_ask=63, spread=1 → join_bid=62
    assert place_order.compute_limit_price(_ob(62, 37), "buy", "midmarket_maker") == 62


# --- join_ask / join_bid ---

def test_join_ask_returns_best_ask():
    # best_ask = 100 - 35 = 65
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "join_ask") == 65


def test_join_bid_returns_best_bid():
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "join_bid") == 62


# --- cross_spread ---

def test_cross_spread_sell_uses_best_bid():
    # sell at bid = immediate taker fill
    assert place_order.compute_limit_price(_ob(62, 35), "sell", "cross_spread") == 62


def test_cross_spread_buy_uses_best_ask():
    # buy at ask = immediate taker fill
    assert place_order.compute_limit_price(_ob(62, 35), "buy", "cross_spread") == 65


# --- error cases ---

def test_empty_yes_book_raises():
    ob = {"orderbook": {"yes": [], "no": [[35, 100]]}}
    with pytest.raises(ValueError, match="best_bid"):
        place_order.compute_limit_price(ob, "sell", "midmarket_maker")


def test_empty_no_book_raises():
    ob = {"orderbook": {"yes": [[62, 100]], "no": []}}
    with pytest.raises(ValueError, match="best_ask"):
        place_order.compute_limit_price(ob, "sell", "midmarket_maker")


def _mock_anthropic(response_json: dict):
    """Return a mock AsyncAnthropic client that returns response_json as a text block."""
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json.dumps(response_json))]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)
    return mock_client


@contextmanager
def patch_haiku(response_json: dict):
    mock_client = _mock_anthropic(response_json)
    with patch("scripts.place_order.anthropic.AsyncAnthropic", return_value=mock_client):
        yield mock_client


@pytest.mark.asyncio
async def test_parse_intent_exit_midmarket():
    haiku_response = {
        "action": "sell", "side": None, "quantity": "all", "amount_dollars": None,
        "pricing": "midmarket_maker", "yes_price": None,
        "cancel_first": False, "cancel_only": False,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("exit full position at midmarket no fees")
    assert result["action"] == "sell"
    assert result["quantity"] == "all"
    assert result["pricing"] == "midmarket_maker"
    assert result["cancel_only"] is False


@pytest.mark.asyncio
async def test_parse_intent_cancel_and_replace():
    haiku_response = {
        "action": "sell", "side": None, "quantity": None, "amount_dollars": None,
        "pricing": None, "yes_price": 65,
        "cancel_first": True, "cancel_only": False,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("cancel and replace at 65 cents")
    assert result["cancel_first"] is True
    assert result["yes_price"] == 65


@pytest.mark.asyncio
async def test_parse_intent_cross_spread():
    haiku_response = {
        "action": "sell", "side": None, "quantity": "all", "amount_dollars": None,
        "pricing": "cross_spread", "yes_price": None,
        "cancel_first": False, "cancel_only": False,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("i need to get filled")
    assert result["pricing"] == "cross_spread"


@pytest.mark.asyncio
async def test_parse_intent_cancel_only():
    haiku_response = {
        "action": None, "side": None, "quantity": None, "amount_dollars": None,
        "pricing": None, "yes_price": None,
        "cancel_first": False, "cancel_only": True,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("cancel all resting orders")
    assert result["cancel_only"] is True


@pytest.mark.asyncio
async def test_parse_intent_buy_with_amount():
    haiku_response = {
        "action": "buy", "side": "yes", "quantity": None, "amount_dollars": 10.0,
        "pricing": "midmarket_maker", "yes_price": None,
        "cancel_first": False, "cancel_only": False,
    }
    with patch_haiku(haiku_response):
        result = await place_order.parse_intent("buy 10 dollars yes at midmarket")
    assert result["action"] == "buy"
    assert result["amount_dollars"] == 10.0
    assert result["side"] == "yes"


@pytest.mark.asyncio
async def test_resolve_quantity_explicit_int():
    assert await place_order.resolve_quantity("KXFOO", 10, "sell", None) == ("sell", 10)


@pytest.mark.asyncio
async def test_resolve_quantity_all_reads_position():
    mock_client = MagicMock()
    mock_client.get_positions = AsyncMock(return_value={
        "market_positions": [
            {"ticker": "KXFOO", "position_fp": "20.00"},
        ]
    })
    side, count = await place_order.resolve_quantity("KXFOO", "all", "sell", mock_client)
    assert side == "yes"
    assert count == 20


@pytest.mark.asyncio
async def test_resolve_quantity_all_no_position_raises():
    mock_client = MagicMock()
    mock_client.get_positions = AsyncMock(return_value={"market_positions": []})
    with pytest.raises(SystemExit):
        await place_order.resolve_quantity("KXFOO", "all", "sell", mock_client)


@pytest.mark.asyncio
async def test_resolve_quantity_amount_dollars():
    # $10 at 50 cents/contract = floor(10 / 0.50) = 20 contracts
    side, count = await place_order.resolve_quantity(
        "KXFOO", None, "buy", None,
        amount_dollars=10.0, yes_price_cents=50,
    )
    assert side == "buy"
    assert count == 20


@pytest.mark.asyncio
async def test_resolve_quantity_missing_raises():
    with pytest.raises(SystemExit):
        await place_order.resolve_quantity("KXFOO", None, "buy", None)


@pytest.mark.asyncio
async def test_cancel_orders_cancels_all_for_ticker():
    from unittest.mock import call
    mock_client = MagicMock()
    mock_client.get_orders = AsyncMock(return_value={"orders": [
        {"order_id": "ord1", "ticker": "KXFOO"},
        {"order_id": "ord2", "ticker": "KXFOO"},
        {"order_id": "ord3", "ticker": "KXBAR"},  # different ticker, should be skipped
    ]})
    mock_client.cancel_order = AsyncMock(return_value={})
    count = await place_order.cancel_orders("KXFOO", mock_client, dry_run=False)
    assert count == 2
    mock_client.cancel_order.assert_has_calls([call("ord1"), call("ord2")], any_order=True)


@pytest.mark.asyncio
async def test_cancel_orders_dry_run_returns_count_without_cancelling():
    mock_client = MagicMock()
    mock_client.get_orders = AsyncMock(return_value={"orders": [
        {"order_id": "ord1", "ticker": "KXFOO"},
    ]})
    mock_client.cancel_order = AsyncMock()
    count = await place_order.cancel_orders("KXFOO", mock_client, dry_run=True)
    assert count == 1
    mock_client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_place_order_op_places_limit_order():
    mock_client = MagicMock()
    mock_client.create_order = AsyncMock(return_value={
        "order": {"order_id": "ord_xyz", "status": "resting", "yes_price_dollars": "0.6400"}
    })
    result = await place_order.place_order_op(
        ticker="KXFOO", action="sell", side="yes", count=20,
        yes_price=64, client=mock_client, dry_run=False,
    )
    assert result["order_id"] == "ord_xyz"
    mock_client.create_order.assert_called_once_with(
        ticker="KXFOO", action="sell", side="yes",
        count=20, order_type="limit", yes_price=64,
    )


@pytest.mark.asyncio
async def test_place_order_op_dry_run_skips_api():
    mock_client = MagicMock()
    mock_client.create_order = AsyncMock()
    result = await place_order.place_order_op(
        ticker="KXFOO", action="sell", side="yes", count=20,
        yes_price=64, client=mock_client, dry_run=True,
    )
    assert result["dry_run"] is True
    mock_client.create_order.assert_not_called()


@pytest.mark.asyncio
async def test_main_dry_run_sell_all_midmarket(capsys):
    """End-to-end dry-run: NL intent + orderbook fetch + position fetch → correct price printed."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get_orderbook = AsyncMock(return_value={
        "orderbook": {"yes": [[62, 100]], "no": [[35, 100]]}
    })
    mock_client.get_positions = AsyncMock(return_value={
        "market_positions": [{"ticker": "KXFOO", "position_fp": "20.00"}]
    })

    haiku_response = {
        "action": "sell", "side": None, "quantity": "all", "amount_dollars": None,
        "pricing": "midmarket_maker", "yes_price": None,
        "cancel_first": False, "cancel_only": False,
    }

    with patch("scripts.place_order.KalshiClient", return_value=mock_client), \
         patch_haiku(haiku_response):
        await place_order._run(
            ticker="KXFOO",
            intent="exit full position at midmarket no fees",
            flags={},
            dry_run=True,
        )

    captured = capsys.readouterr()
    assert "[DRY-RUN]" in captured.out
    assert "yes_price=64" in captured.out   # ceil((62+65)/2) = 64
    assert "20 contracts" in captured.out
