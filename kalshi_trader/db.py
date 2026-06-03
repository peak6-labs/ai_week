"""Supabase persistence layer.

Project: ai_week — https://xhyqdrhrwgebidvsnwbx.supabase.co
ONLY this project may be accessed. URL is validated on first connection.

Rules:
- No DELETE operations anywhere in this module.
- All writes use INSERT or UPSERT (ON CONFLICT DO UPDATE).
- Client is lazily initialized from SUPABASE_URL + SUPABASE_SERVICE_KEY env vars.
- Service role key bypasses RLS — keep it out of logs and client-side code.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

# Route SSL through the system trust store so the Supabase httpx client works
# behind the corporate proxy (Zscaler self-signed cert). inject_into_ssl()
# patches the stdlib ssl module httpx builds its default context from.
try:
    import truststore as _truststore
    _truststore.inject_into_ssl()
except Exception:  # pragma: no cover - truststore optional
    pass

from supabase import AsyncClient, acreate_client

from kalshi_trader import config
from kalshi_trader.models import OrderResult, RiskDecision, SignalEstimate, TradeIdea

logger = logging.getLogger(__name__)

# Expected project ref — validated before any connection is made.
_EXPECTED_REF = "xhyqdrhrwgebidvsnwbx"

_client: Optional[AsyncClient] = None


async def _get_client() -> AsyncClient:
    """Return the singleton async Supabase client, initializing on first call.

    Raises RuntimeError if SUPABASE_URL or SUPABASE_SERVICE_KEY are not set,
    or if the URL does not point to the ai_week project.
    """
    global _client
    if _client is not None:
        return _client

    url = config.SUPABASE_URL
    key = config.SUPABASE_SERVICE_KEY

    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
        )

    # Hard safety check — refuse to connect to any project other than ai_week.
    if _EXPECTED_REF not in url:
        raise RuntimeError(
            f"SUPABASE_URL does not point to the ai_week project ({_EXPECTED_REF}). "
            f"Got: {url!r}. Refusing to connect."
        )

    _client = await acreate_client(url, key)
    return _client


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------

async def insert_signal(
    signal: SignalEstimate,
    ticker: str,
    trade_id: str | None = None,
) -> str:
    """Insert a SignalEstimate row. Returns the new signal UUID.

    Call this immediately after a signal is generated — before any
    consensus or risk checks. trade_id is null until a trade is executed.
    """
    client = await _get_client()
    row = {
        "ticker": ticker,
        "source": signal.source,
        "probability": signal.probability,
        "uncertainty": signal.uncertainty,
        "weight": signal.weight,
        "data_issued_at": signal.data_issued_at.isoformat(),
        "metadata": signal.metadata,
        "trade_id": trade_id,
    }
    resp = await client.table("signals").insert(row).execute()
    return resp.data[0]["id"]


async def link_signals_to_trade(signal_ids: list[str], trade_id: str) -> None:
    """Set trade_id on a batch of signals after a trade is executed.

    Caller must ensure signal_ids contains at most one signal per source —
    the UNIQUE (trade_id, source) index will raise if two signals with the
    same source are linked to the same trade.
    """
    if not signal_ids:
        return
    client = await _get_client()
    await (
        client.table("signals")
        .update({"trade_id": trade_id})
        .in_("id", signal_ids)
        .execute()
    )


async def resolve_market(ticker: str, resolved_yes: bool) -> None:
    """Record market resolution outcome on all signals for a ticker.

    Sets market_resolved_yes and computes brier_score = (probability - outcome)^2.
    Only updates signals where market_resolved_yes IS NULL (idempotent).
    """
    client = await _get_client()
    outcome = 1.0 if resolved_yes else 0.0

    # Fetch unresolved signals for this ticker.
    resp = await (
        client.table("signals")
        .select("id, probability")
        .eq("ticker", ticker)
        .is_("market_resolved_yes", "null")
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return

    # Update each signal with its brier score. Number of signals per market
    # is small (~5–15), so individual updates are fine here.
    for row in rows:
        prob = float(row["probability"])
        brier = round((prob - outcome) ** 2, 6)
        await (
            client.table("signals")
            .update({
                "market_resolved_yes": resolved_yes,
                "brier_score": brier,
            })
            .eq("id", row["id"])
            .execute()
        )


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------

async def insert_trade(
    idea: TradeIdea,
    result: OrderResult,
    decision: RiskDecision,
    contracts: int,
) -> str:
    """Insert a trade row after execution. Returns the new trade UUID."""
    client = await _get_client()
    row = {
        "ticker": idea.ticker,
        "side": idea.side.value,
        "action": idea.action.value,
        "contracts": contracts,
        "entry_price_cents": idea.market_price,
        "fill_price_cents": result.fill_price,
        "size_dollars": result.size_dollars,
        "suggested_size_dollars": decision.approved_size_dollars,
        "status": result.status,
        "kalshi_order_id": result.order_id or None,
        "agent_id": idea.agent_id,
        "confidence": idea.confidence,
        "reasoning": idea.reasoning,
        "category": idea.category,
    }
    resp = await client.table("trades").insert(row).execute()
    return resp.data[0]["id"]


async def insert_reviewed_idea(
    idea: dict,
    decision: str,
) -> str:
    """Save a manually-reviewed trade idea that was NOT executed.

    paper_only is always True — this table records human evaluation of
    orchestrator output before the executor is wired up. It must never
    be confused with actual trades.

    Args:
        idea: Serializable dict with keys: ticker, side, confidence,
              market_price, suggested_size_dollars, reasoning,
              signal_sources, category, agent_id.
        decision: "approved" or "rejected"

    Returns:
        The new row UUID.
    """
    client = await _get_client()
    row = {
        "ticker": idea.get("ticker", ""),
        "side": idea.get("side", ""),
        "confidence": idea.get("confidence"),
        "market_price_cents": idea.get("market_price"),
        "suggested_size_dollars": idea.get("suggested_size_dollars"),
        "reasoning": idea.get("reasoning", ""),
        "signal_sources": idea.get("signal_sources", []),
        "category": idea.get("category", ""),
        "agent_id": idea.get("agent_id", ""),
        "decision": decision,
        "paper_only": True,  # ALWAYS True — reviewed but not executed
        "reviewed_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    resp = await client.table("reviewed_ideas").insert(row).execute()
    return resp.data[0]["id"]


# ---------------------------------------------------------------------------
# paper-trade calibration (recommendations + marks + cycles)
# Tables are created by sql/001_paper_calibration.sql. These helpers no-op
# gracefully (caller wraps in try/except) until that migration is applied.
# ---------------------------------------------------------------------------

async def insert_recommendation(rec: dict) -> None:
    """Insert one paper recommendation. ``rec`` carries the local rec_id as id."""
    client = await _get_client()
    row = {
        "id": rec["rec_id"],
        "cycle_ts": rec.get("cycle_ts", ""),
        "ticker": rec["ticker"],
        "side": rec["side"],
        "entry_price_cents": rec["entry_price_cents"],
        "predicted_prob": rec.get("predicted_prob"),
        "edge_cents": rec.get("edge_cents"),
        "n_sources": rec.get("n_sources"),
        "sources": rec.get("sources", []),
        "category": rec.get("category", ""),
        "suggested_size_dollars": rec.get("suggested_size_dollars"),
        "status": "open",
        "paper_only": True,
    }
    await client.table("recommendations").upsert(row, on_conflict="id").execute()


async def insert_recommendation_mark(recommendation_id: str, mark: dict) -> None:
    """Insert one mark-to-market check for a recommendation."""
    client = await _get_client()
    await client.table("recommendation_marks").insert({
        "recommendation_id": recommendation_id,
        "current_value_cents": mark.get("current_value_cents"),
        "pnl_cents": mark.get("pnl_cents"),
        "would_profit": mark.get("would_profit"),
        "resolved": bool(mark.get("resolved")),
    }).execute()


async def resolve_recommendation(recommendation_id: str) -> None:
    """Mark a recommendation resolved (idempotent)."""
    client = await _get_client()
    await (
        client.table("recommendations")
        .update({"status": "resolved"})
        .eq("id", recommendation_id)
        .execute()
    )


async def upsert_cycle(cycle_ts: str, stats: dict) -> None:
    """Record per-cycle pipeline stats (markets scored, candidates, ideas)."""
    client = await _get_client()
    row = {"cycle_ts": cycle_ts, **stats}
    await client.table("cycles").upsert(row, on_conflict="cycle_ts").execute()


async def close_trade(
    trade_id: str,
    exit_reason: str,
    realized_pnl_dollars: float,
) -> None:
    """Mark an opening trade as closed with its exit reason and P&L."""
    client = await _get_client()
    await (
        client.table("trades")
        .update({
            "exit_reason": exit_reason,
            "realized_pnl_dollars": realized_pnl_dollars,
            "closed_at": datetime.now(tz=timezone.utc).isoformat(),
        })
        .eq("id", trade_id)
        .execute()
    )


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

async def open_position(
    idea: TradeIdea,
    result: OrderResult,
    trade_id: str,
    contracts: int,
) -> str:
    """Insert an open position record after a BUY trade fills.

    Returns the new position UUID.
    The unique partial index on (ticker WHERE closed_at IS NULL) prevents
    opening a second position on the same market.
    """
    client = await _get_client()
    opened_at = result.created_at
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)

    row = {
        "ticker": idea.ticker,
        "side": idea.side.value,
        "contracts": contracts,
        "category": idea.category,
        "avg_entry_price_cents": result.fill_price,
        "opened_at": opened_at.isoformat(),
        "opening_trade_id": trade_id,
    }
    resp = await client.table("positions").insert(row).execute()
    return resp.data[0]["id"]


async def close_position(
    ticker: str,
    closing_trade_id: str,
    realized_pnl_dollars: float,
    exit_reason: str,
) -> str | None:
    """Mark the open position for a ticker as closed.

    Also updates the opening trade with exit_reason and realized P&L.
    Returns the opening_trade_id so the caller can log it, or None if
    no open position was found.
    """
    client = await _get_client()

    # Fetch the open position to get opening_trade_id.
    resp = await (
        client.table("positions")
        .select("id, opening_trade_id")
        .eq("ticker", ticker)
        .is_("closed_at", "null")
        .execute()
    )
    rows = resp.data or []
    if not rows:
        logger.warning("close_position: no open position found for ticker %s", ticker)
        return None

    position = rows[0]
    position_id = position["id"]
    opening_trade_id = position["opening_trade_id"]
    now = datetime.now(tz=timezone.utc).isoformat()

    # Update position — filter on closed_at IS NULL so that if two coroutines
    # race through the SELECT above, only the first UPDATE wins. The second
    # gets back empty data and returns None without touching the opening trade.
    resp = await (
        client.table("positions")
        .update({
            "closed_at": now,
            "closing_trade_id": closing_trade_id,
            "realized_pnl_dollars": realized_pnl_dollars,
        })
        .eq("id", position_id)
        .is_("closed_at", "null")
        .execute()
    )
    if not resp.data:
        logger.warning(
            "close_position: position %s for ticker %s already closed (race condition)",
            position_id, ticker,
        )
        return None

    # Update the opening trade only after confirming we won the UPDATE race.
    await close_trade(opening_trade_id, exit_reason, realized_pnl_dollars)

    return opening_trade_id


# ---------------------------------------------------------------------------
# polymarket_markets
# ---------------------------------------------------------------------------

_POLYMARKET_BATCH_SIZE = 500


def _prepare_polymarket_row(market: dict) -> dict:
    """Map a Polymarket Gamma API market dict to a polymarket_markets row.

    Handles the outcomePrices field, which the API returns as a JSON string.
    """
    # outcomePrices comes as a string: "[0.45, 0.55]" — parse before storing.
    raw_prices = market.get("outcomePrices")
    if isinstance(raw_prices, str):
        try:
            outcome_prices = json.loads(raw_prices)
        except (json.JSONDecodeError, ValueError):
            outcome_prices = None
    elif isinstance(raw_prices, list):
        outcome_prices = raw_prices
    else:
        outcome_prices = None

    yes_price: float | None = None
    if isinstance(outcome_prices, list) and outcome_prices:
        try:
            yes_price = float(outcome_prices[0])
        except (ValueError, TypeError):
            pass

    # clob_token_ids: API returns as JSON string in some versions.
    raw_tokens = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(raw_tokens, str):
        try:
            clob_token_ids = json.loads(raw_tokens)
        except (json.JSONDecodeError, ValueError):
            clob_token_ids = None
    elif isinstance(raw_tokens, list):
        clob_token_ids = raw_tokens
    else:
        clob_token_ids = None

    # end_date: ISO string from API.
    raw_end = market.get("endDate") or market.get("end_date")
    end_date: str | None = None
    if raw_end:
        # Normalise to UTC ISO format.
        end_date = raw_end.replace("Z", "+00:00") if isinstance(raw_end, str) else None

    return {
        "condition_id": market["conditionId"],
        "question": market["question"],
        "yes_price": yes_price,
        "active": bool(market.get("active", True)),
        "closed": bool(market.get("closed", False)),
        "volume_24h": float(v) if (v := market.get("volume24hr")) is not None else None,
        "outcome_prices": outcome_prices,
        "clob_token_ids": clob_token_ids,
        "slug": market.get("slug") or None,
        "best_bid": float(market["bestBid"]) if market.get("bestBid") else None,
        "best_ask": float(market["bestAsk"]) if market.get("bestAsk") else None,
        "end_date": end_date,
        "neg_risk": bool(market.get("negRisk", False)),
        "last_refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
    }


async def upsert_polymarket_markets(markets: list[dict]) -> int:
    """Upsert the Polymarket catalog in batches of 500.

    Returns the total number of rows upserted.
    Skips rows that fail preparation (missing conditionId or question).
    """
    if not markets:
        return 0

    client = await _get_client()
    rows = []
    for market in markets:
        if not market.get("conditionId") or not market.get("question"):
            continue
        try:
            rows.append(_prepare_polymarket_row(market))
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Skipping malformed Polymarket market: %s", exc)

    if not rows:
        return 0

    total = 0
    for i in range(0, len(rows), _POLYMARKET_BATCH_SIZE):
        batch = rows[i : i + _POLYMARKET_BATCH_SIZE]
        try:
            await (
                client.table("polymarket_markets")
                .upsert(batch, on_conflict="condition_id")
                .execute()
            )
            total += len(batch)
        except Exception as exc:
            logger.warning(
                "polymarket_markets upsert failed for batch %d–%d: %s",
                i, i + len(batch) - 1, exc,
            )

    return total


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

async def get_open_positions() -> list[dict]:
    """Return all positions where closed_at IS NULL."""
    client = await _get_client()
    resp = await (
        client.table("positions")
        .select("*")
        .is_("closed_at", "null")
        .execute()
    )
    return resp.data or []


async def get_recent_trades(limit: int = 50) -> list[dict]:
    """Return the most recent trades, newest first."""
    client = await _get_client()
    resp = await (
        client.table("trades")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


async def get_signals_for_trade(trade_id: str) -> list[dict]:
    """Return all signals linked to a specific trade."""
    client = await _get_client()
    resp = await (
        client.table("signals")
        .select("*")
        .eq("trade_id", trade_id)
        .execute()
    )
    return resp.data or []
