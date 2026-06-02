"""Unit tests for KalshiWebSocketClient — all network calls mocked."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import aiohttp
import pytest

from kalshi_trader.external.kalshi_ws import KalshiWebSocketClient
from kalshi_trader.orderbook import OrderBookState

TICKER = "INXY-25DEC31-T49999.99"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_msg(data: dict) -> MagicMock:
    """Create a mock aiohttp WSMessage with type=TEXT."""
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.TEXT
    msg.data = json.dumps(data)
    return msg


def _close_msg() -> MagicMock:
    """Create a mock aiohttp WSMessage with type=CLOSE."""
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.CLOSE
    msg.data = None
    return msg


# ---------------------------------------------------------------------------
# _handle — orderbook_snapshot
# ---------------------------------------------------------------------------

def test_handle_snapshot_updates_state():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    msg = {
        "type": "orderbook_snapshot",
        "market_ticker": TICKER,
        "yes": [{"price": "52", "quantity": "10"}, {"price": "48", "quantity": "5"}],
        "no": [{"price": "55", "quantity": "8"}],
    }
    client._handle(msg)
    assert state._bids[TICKER] == {52: 10, 48: 5}
    assert state._asks[TICKER] == {55: 8}


def test_handle_snapshot_replaces_old_state():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    # Apply old snapshot first
    client._handle({
        "type": "orderbook_snapshot",
        "market_ticker": TICKER,
        "yes": [{"price": "40", "quantity": "99"}],
        "no": [],
    })
    # Apply new snapshot
    client._handle({
        "type": "orderbook_snapshot",
        "market_ticker": TICKER,
        "yes": [{"price": "52", "quantity": "10"}],
        "no": [{"price": "55", "quantity": "8"}],
    })
    assert 40 not in state._bids[TICKER]
    assert state._bids[TICKER] == {52: 10}


def test_handle_snapshot_empty_books():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({
        "type": "orderbook_snapshot",
        "market_ticker": TICKER,
        "yes": [],
        "no": [],
    })
    assert state._bids[TICKER] == {}
    assert state._asks[TICKER] == {}


# ---------------------------------------------------------------------------
# _handle — orderbook_delta
# ---------------------------------------------------------------------------

def test_handle_delta_adds_bid_level():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({
        "type": "orderbook_delta",
        "market_ticker": TICKER,
        "side": "yes",
        "price": "55",
        "delta": "10",
    })
    assert state._bids[TICKER][55] == 10


def test_handle_delta_adds_ask_level():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({
        "type": "orderbook_delta",
        "market_ticker": TICKER,
        "side": "no",
        "price": "60",
        "delta": "7",
    })
    assert state._asks[TICKER][60] == 7


def test_handle_delta_removes_level_on_zero():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    # Add then remove
    client._handle({
        "type": "orderbook_delta",
        "market_ticker": TICKER,
        "side": "yes",
        "price": "55",
        "delta": "10",
    })
    client._handle({
        "type": "orderbook_delta",
        "market_ticker": TICKER,
        "side": "yes",
        "price": "55",
        "delta": "0",
    })
    assert 55 not in state._bids[TICKER]


def test_handle_delta_updates_level():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({
        "type": "orderbook_delta",
        "market_ticker": TICKER,
        "side": "yes",
        "price": "55",
        "delta": "10",
    })
    client._handle({
        "type": "orderbook_delta",
        "market_ticker": TICKER,
        "side": "yes",
        "price": "55",
        "delta": "25",
    })
    assert state._bids[TICKER][55] == 25


# ---------------------------------------------------------------------------
# _handle — trade
# ---------------------------------------------------------------------------

def test_handle_trade_records_trade():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({
        "type": "trade",
        "market_ticker": TICKER,
        "count": "15",
    })
    trades = list(state._trades[TICKER])
    assert len(trades) == 1
    _ts, size = trades[0]
    assert size == 15


def test_handle_trade_accumulates_multiple():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    for count in [5, 10, 20]:
        client._handle({
            "type": "trade",
            "market_ticker": TICKER,
            "count": str(count),
        })
    trades = list(state._trades[TICKER])
    assert len(trades) == 3
    sizes = [s for _, s in trades]
    assert sizes == [5, 10, 20]


# ---------------------------------------------------------------------------
# _handle — unknown type does not raise
# ---------------------------------------------------------------------------

def test_handle_unknown_type_does_not_raise():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    # Should silently ignore unknown message types
    client._handle({"type": "totally_unknown_type", "market_ticker": TICKER})


def test_handle_subscribed_does_not_raise():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({"type": "subscribed", "market_ticker": TICKER})


def test_handle_error_does_not_raise():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({"type": "error", "code": 400, "msg": "bad request"})


def test_handle_empty_message_does_not_raise():
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({})


# ---------------------------------------------------------------------------
# State is shared correctly between _handle calls
# ---------------------------------------------------------------------------

def test_state_shared_across_handle_calls():
    """Snapshot + delta + trade all land on the same shared state object."""
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)

    client._handle({
        "type": "orderbook_snapshot",
        "market_ticker": TICKER,
        "yes": [{"price": "50", "quantity": "10"}],
        "no": [{"price": "55", "quantity": "5"}],
    })
    client._handle({
        "type": "orderbook_delta",
        "market_ticker": TICKER,
        "side": "yes",
        "price": "52",
        "delta": "8",
    })
    client._handle({
        "type": "trade",
        "market_ticker": TICKER,
        "count": "3",
    })

    assert state._bids[TICKER] == {50: 10, 52: 8}
    assert state._asks[TICKER] == {55: 5}
    assert len(state._trades[TICKER]) == 1


def test_state_injected_externally_is_used():
    """Verify that a pre-existing state object is used (not a fresh one)."""
    state = OrderBookState()
    state.apply_delta(TICKER, "yes", 40, 99)  # pre-existing data

    client = KalshiWebSocketClient(tickers=[TICKER], state=state)
    client._handle({
        "type": "orderbook_delta",
        "market_ticker": TICKER,
        "side": "yes",
        "price": "52",
        "delta": "5",
    })

    # Both the pre-existing level and the new one should be present
    assert state._bids[TICKER][40] == 99
    assert state._bids[TICKER][52] == 5


# ---------------------------------------------------------------------------
# run() — graceful stop via mock WS
# ---------------------------------------------------------------------------

def _make_mock_ws(messages: list) -> MagicMock:
    """Build a mock WS where ws.receive() returns messages in sequence."""
    mock_ws = AsyncMock()
    mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_ws.__aexit__ = AsyncMock(return_value=False)
    mock_ws.send_str = AsyncMock()
    mock_ws.receive = AsyncMock(side_effect=messages)
    return mock_ws


def _make_mock_session(mock_ws) -> AsyncMock:
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.ws_connect = MagicMock(return_value=mock_ws)
    mock_session.close = AsyncMock()
    return mock_session


@pytest.mark.asyncio
async def test_run_processes_one_message_then_closes():
    """run() should process a message and exit cleanly when CLOSE is received."""
    state = OrderBookState()
    client = KalshiWebSocketClient(tickers=[TICKER], state=state)

    snapshot_msg = _text_msg({
        "type": "orderbook_snapshot",
        "market_ticker": TICKER,
        "yes": [{"price": "50", "quantity": "10"}],
        "no": [{"price": "55", "quantity": "5"}],
    })
    mock_ws = _make_mock_ws([snapshot_msg, _close_msg()])
    mock_session = _make_mock_session(mock_ws)

    with (
        patch("kalshi_trader.external.kalshi_ws._ws_headers", return_value={}),
        patch("kalshi_trader.external.kalshi_ws._ssl_context", return_value=None),
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch("aiohttp.TCPConnector"),
    ):
        async def _stop_after_delay():
            await asyncio.sleep(0.05)
            await client.stop()

        await asyncio.gather(client.run(), _stop_after_delay())

    assert state._bids[TICKER] == {50: 10}
    assert state._asks[TICKER] == {55: 5}


@pytest.mark.asyncio
async def test_run_sends_subscribe_message():
    """run() should send a subscribe message after connecting."""
    tickers = [TICKER, "OTHER-TICKER"]
    client = KalshiWebSocketClient(tickers=tickers)

    mock_ws = _make_mock_ws([_close_msg()])
    mock_session = _make_mock_session(mock_ws)

    with (
        patch("kalshi_trader.external.kalshi_ws._ws_headers", return_value={}),
        patch("kalshi_trader.external.kalshi_ws._ssl_context", return_value=None),
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch("aiohttp.TCPConnector"),
    ):
        async def _stop_after_delay():
            await asyncio.sleep(0.05)
            await client.stop()

        await asyncio.gather(client.run(), _stop_after_delay())

    mock_ws.send_str.assert_called_once()
    sent_payload = json.loads(mock_ws.send_str.call_args[0][0])
    assert sent_payload["cmd"] == "subscribe"
    assert set(sent_payload["params"]["market_tickers"]) == set(tickers)
    assert "orderbook_delta" in sent_payload["params"]["channels"]


@pytest.mark.asyncio
async def test_run_reconnects_on_watchdog_timeout():
    """Watchdog timeout on ws.receive() should trigger a reconnect."""
    client = KalshiWebSocketClient(tickers=[TICKER])

    call_count = 0

    async def _receive_timeout():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise asyncio.TimeoutError  # simulate silent proxy drop
        return _close_msg()

    mock_ws = AsyncMock()
    mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_ws.__aexit__ = AsyncMock(return_value=False)
    mock_ws.send_str = AsyncMock()
    mock_ws.receive = _receive_timeout
    mock_session = _make_mock_session(mock_ws)

    with (
        patch("kalshi_trader.external.kalshi_ws._ws_headers", return_value={}),
        patch("kalshi_trader.external.kalshi_ws._ssl_context", return_value=None),
        patch("kalshi_trader.external.kalshi_ws._WATCHDOG_TIMEOUT", 0.01),
        patch("kalshi_trader.external.kalshi_ws._RECONNECT_DELAY", 0.01),
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch("aiohttp.TCPConnector"),
    ):
        async def _stop_after_delay():
            await asyncio.sleep(0.15)
            await client.stop()

        await asyncio.gather(client.run(), _stop_after_delay())

    # Should have connected at least twice (initial + reconnect after timeout)
    assert mock_ws.send_str.call_count >= 2


@pytest.mark.asyncio
async def test_stop_sets_running_false():
    """stop() should set _running to False and allow run() to exit."""
    client = KalshiWebSocketClient(tickers=[TICKER])
    client._running = True
    client._session = None
    await client.stop()
    assert client._running is False
