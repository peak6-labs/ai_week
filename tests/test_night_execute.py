"""Tests for scripts/night_execute.py.

apply_rules() is a pure function — tested directly with no mocking.
run() is async — tests mock KalshiClient to avoid real API calls.
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

night_execute = importlib.import_module("scripts.night_execute")
apply_rules = night_execute.apply_rules


def _session(trades_placed: int = 0, dollars_spent: float = 0.0) -> dict:
    return {
        "started_at": "2026-06-03T22:00:00Z",
        "trades_placed": trades_placed,
        "dollars_spent": dollars_spent,
        "tickers_traded": [],
    }


def _candidate(
    ticker: str = "KXTEST-1",
    side: str = "yes",
    confidence: float = 0.65,
    market_price: float = 55.0,
    category: str = "politics",
    hours_to_close: float = 24.0,
) -> dict:
    return {
        "ticker": ticker,
        "side": side,
        "confidence": confidence,
        "market_price": market_price,
        "category": category,
        "hours_to_close": hours_to_close,
        "signal_sources": ["polymarket_price", "kalshi_bias"],
        "reasoning": "Test reasoning",
        "agent_id": "night_mode",
        "selection_summary": "Test",
    }


def _make_client(order_id: str = "ord_night1", status: str = "resting") -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.create_order = AsyncMock(
        return_value={"order": {"order_id": order_id, "status": status}}
    )
    return client


# ---------------------------------------------------------------------------
# apply_rules — pure function tests
# ---------------------------------------------------------------------------

def test_apply_rules_passes_valid_candidate():
    assert apply_rules(_candidate(), _session()) is None


def test_apply_rules_rejects_on_trade_cap():
    assert apply_rules(_candidate(), _session(trades_placed=10)) == "session_cap_reached"


def test_apply_rules_rejects_on_dollar_cap():
    assert apply_rules(_candidate(), _session(dollars_spent=100.0)) == "session_cap_reached"


def test_apply_rules_session_cap_checked_before_edge():
    """Session cap takes priority — even a bad-edge idea is rejected with cap reason."""
    low_edge = _candidate(confidence=0.55, market_price=55.0)  # edge=0
    assert apply_rules(low_edge, _session(trades_placed=10)) == "session_cap_reached"


def test_apply_rules_rejects_insufficient_edge():
    # edge = 0.55 - 0.55 = 0.00 < 0.05
    assert apply_rules(_candidate(confidence=0.55, market_price=55.0), _session()) == "edge_insufficient"


def test_apply_rules_passes_exact_edge_threshold():
    # edge = 0.60 - 0.55 = 0.05 — exactly at threshold, should pass (< not <=)
    assert apply_rules(_candidate(confidence=0.60, market_price=55.0), _session()) is None


def test_apply_rules_rejects_unquoted_zero_price():
    assert apply_rules(_candidate(market_price=0.0), _session()) == "unquoted"


def test_apply_rules_rejects_unquoted_100_price():
    assert apply_rules(_candidate(market_price=100.0), _session()) == "unquoted"


def test_apply_rules_rejects_weather_under_2h():
    candidate = _candidate(category="climate and weather", hours_to_close=1.5)
    assert apply_rules(candidate, _session()) == "weather_settlement_proximity"


def test_apply_rules_passes_weather_over_2h():
    candidate = _candidate(category="climate and weather", hours_to_close=3.0)
    assert apply_rules(candidate, _session()) is None


def test_apply_rules_passes_weather_no_hours():
    """Weather market with no hours_to_close — skip the gate rather than reject."""
    candidate = _candidate(category="climate and weather", hours_to_close=None)
    assert apply_rules(candidate, _session()) is None


def test_apply_rules_rejects_love_island_under_2h():
    """Love island is excluded entirely — not just gated on settlement time."""
    candidate = _candidate(category="love island", hours_to_close=0.5)
    assert apply_rules(candidate, _session()) == "love_island_excluded"


def test_apply_rules_passes_politics_under_2h():
    candidate = _candidate(category="politics", hours_to_close=0.1)
    assert apply_rules(candidate, _session()) is None


def test_apply_rules_passes_sports_at_zero_hours():
    candidate = _candidate(category="sports", hours_to_close=0.0)
    assert apply_rules(candidate, _session()) is None


def test_apply_rules_rejects_love_island():
    """Love island markets are always excluded from night-mode execution."""
    candidate = _candidate(category="love island", hours_to_close=24.0)
    assert apply_rules(candidate, _session()) == "love_island_excluded"


def test_apply_rules_rejects_love_island_regardless_of_edge():
    """Love island exclusion fires even on a high-edge candidate."""
    candidate = _candidate(category="love island", confidence=0.95, market_price=10.0)
    assert apply_rules(candidate, _session()) == "love_island_excluded"


# ---------------------------------------------------------------------------
# run() — integration tests with mocked KalshiClient
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_executes_valid_candidate(tmp_path):
    """Valid candidate triggers a buy order at $10 flat."""
    client = _make_client()
    session_file = str(tmp_path / "session.json")

    with patch("scripts.night_execute.KalshiClient", return_value=client):
        results = await night_execute.run(
            candidates=[_candidate()],
            session_file=session_file,
            cycle_ts="20260603T220000Z",
            dry_run=False,
            log_dir=str(tmp_path),
        )

    assert len(results) == 1
    record = results[0]
    assert record["rejection_reason"] is None
    assert record["order_id"] == "ord_night1"
    assert record["order_status"] == "resting"
    assert record["session_trade_number"] == 1
    assert record["session_dollars_spent"] == 10.0

    # YES side, market_price=55 → yes_price=55, count=floor(1000/55)=18
    client.create_order.assert_awaited_once_with(
        ticker="KXTEST-1",
        action="buy",
        side="yes",
        count=18,
        order_type="limit",
        yes_price=55,
    )


@pytest.mark.asyncio
async def test_run_no_side_yes_price_complement(tmp_path):
    """NO-side order: yes_price = round(100 - market_price)."""
    client = _make_client()
    session_file = str(tmp_path / "session.json")
    # NO side: market_price=40 → yes_price=60, count=floor(1000/40)=25
    candidate = _candidate(side="no", market_price=40.0, confidence=0.70)

    with patch("scripts.night_execute.KalshiClient", return_value=client):
        await night_execute.run(
            candidates=[candidate],
            session_file=session_file,
            cycle_ts="20260603T220000Z",
            dry_run=False,
            log_dir=str(tmp_path),
        )

    client.create_order.assert_awaited_once_with(
        ticker="KXTEST-1",
        action="buy",
        side="no",
        count=25,
        order_type="limit",
        yes_price=60,
    )


@pytest.mark.asyncio
async def test_run_dry_run_no_order(tmp_path):
    """dry_run=True skips order placement."""
    client = _make_client()
    session_file = str(tmp_path / "session.json")

    with patch("scripts.night_execute.KalshiClient", return_value=client):
        results = await night_execute.run(
            candidates=[_candidate()],
            session_file=session_file,
            cycle_ts="20260603T220000Z",
            dry_run=True,
            log_dir=str(tmp_path),
        )

    client.create_order.assert_not_awaited()
    assert results[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_run_session_cap_stops_after_10th_trade(tmp_path):
    """After 10 trades the 11th candidate is rejected with session_cap_reached."""
    session_file = str(tmp_path / "session.json")
    (tmp_path / "session.json").write_text(json.dumps(
        {"started_at": "2026-06-03T22:00:00Z", "trades_placed": 9,
         "dollars_spent": 90.0, "tickers_traded": []}
    ))
    client = _make_client()
    candidates = [_candidate(ticker=f"KXTEST-{i}") for i in range(2)]

    with patch("scripts.night_execute.KalshiClient", return_value=client):
        results = await night_execute.run(
            candidates=candidates,
            session_file=session_file,
            cycle_ts="20260603T220000Z",
            dry_run=False,
            log_dir=str(tmp_path),
        )

    assert results[0]["rejection_reason"] is None
    assert results[0]["session_trade_number"] == 10
    assert results[1]["rejection_reason"] == "session_cap_reached"
    assert client.create_order.await_count == 1


@pytest.mark.asyncio
async def test_run_session_state_persisted(tmp_path):
    """Session file updated after execution."""
    session_file = str(tmp_path / "session.json")
    client = _make_client()

    with patch("scripts.night_execute.KalshiClient", return_value=client):
        await night_execute.run(
            candidates=[_candidate()],
            session_file=session_file,
            cycle_ts="20260603T220000Z",
            dry_run=False,
            log_dir=str(tmp_path),
        )

    saved = json.loads(Path(session_file).read_text())
    assert saved["trades_placed"] == 1
    assert saved["dollars_spent"] == 10.0
    assert "KXTEST-1" in saved["tickers_traded"]


@pytest.mark.asyncio
async def test_run_creates_session_if_missing(tmp_path):
    """No session file → initialized with zeroes on first run."""
    session_file = str(tmp_path / "session.json")
    assert not Path(session_file).exists()
    client = _make_client()

    with patch("scripts.night_execute.KalshiClient", return_value=client):
        await night_execute.run(
            candidates=[_candidate()],
            session_file=session_file,
            cycle_ts="20260603T220000Z",
            dry_run=False,
            log_dir=str(tmp_path),
        )

    assert Path(session_file).exists()
    saved = json.loads(Path(session_file).read_text())
    assert saved["trades_placed"] == 1


@pytest.mark.asyncio
async def test_run_jsonl_appended(tmp_path):
    """Each processed candidate is appended to the JSONL log."""
    session_file = str(tmp_path / "session.json")
    client = _make_client()

    with patch("scripts.night_execute.KalshiClient", return_value=client):
        await night_execute.run(
            candidates=[
                _candidate(),
                _candidate(ticker="KXTEST-2", confidence=0.55, market_price=55.0),
            ],
            session_file=session_file,
            cycle_ts="20260603T220000Z",
            dry_run=False,
            log_dir=str(tmp_path),
        )

    jsonl_files = list(tmp_path.glob("night-mode-*.jsonl"))
    assert len(jsonl_files) == 1
    lines = jsonl_files[0].read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["ticker"] == "KXTEST-1"
    assert json.loads(lines[1])["rejection_reason"] == "edge_insufficient"


@pytest.mark.asyncio
async def test_run_order_failure_continues(tmp_path):
    """One order failure does not stop processing of subsequent candidates."""
    session_file = str(tmp_path / "session.json")
    client = _make_client()
    client.create_order = AsyncMock(side_effect=[
        Exception("API error"),
        {"order": {"order_id": "ord_2", "status": "resting"}},
    ])

    with patch("scripts.night_execute.KalshiClient", return_value=client):
        results = await night_execute.run(
            candidates=[_candidate(ticker="KXTEST-1"), _candidate(ticker="KXTEST-2")],
            session_file=session_file,
            cycle_ts="20260603T220000Z",
            dry_run=False,
            log_dir=str(tmp_path),
        )

    assert "order_failed" in results[0]["rejection_reason"]
    assert results[1]["order_id"] == "ord_2"
    assert client.create_order.await_count == 2
