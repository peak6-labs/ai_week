"""Paper-trade tracking — record recommendations, mark them to market later.

No real execution, ever. This is the empirical calibration loop: every
recommendation is logged with its entry price and predicted probability, then on
later cycles we fetch the current price and compute the would-be P&L. The record
of marks tells us whether a signal mix is actually predictive, which drives
weight tuning.

Source of truth is a local JSONL store (robust, no schema dependency). Signal
estimates are additionally persisted to the Supabase `signals` table, which
auto-computes a Brier score per source when the market resolves.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

_PAPER_DIR = Path(__file__).resolve().parent.parent / "data" / "paper"
_RECS_FILE = _PAPER_DIR / "recommendations.jsonl"
_MARKS_FILE = _PAPER_DIR / "marks.jsonl"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def entry_price_cents(side: str, yes_bid: float, yes_ask: float) -> float:
    """Taker cost (cents) to open the chosen side: buy YES at ask, buy NO at 100-bid."""
    if side == "yes":
        return float(yes_ask)
    return 100.0 - float(yes_bid)


def mark_value_cents(side: str, yes_bid: float, yes_ask: float) -> float:
    """Conservative current value (cents) of an open position if closed now.

    Sell YES into the bid; close NO by selling at 100-ask. Marks at the price you
    could realistically exit at, not the mid.
    """
    if side == "yes":
        return float(yes_bid)
    return 100.0 - float(yes_ask)


def settle_value_cents(side: str, resolved_yes: bool) -> float:
    """Final value (cents) once the market resolves: 100 if the side won, else 0."""
    won = (side == "yes" and resolved_yes) or (side == "no" and not resolved_yes)
    return 100.0 if won else 0.0


def compute_mark(
    side: str,
    entry_cents: float,
    yes_bid: float | None,
    yes_ask: float | None,
    resolved_yes: bool | None,
) -> dict:
    """Compute would-be P&L (cents per contract) for an open recommendation.

    Returns dict with current_value_cents, pnl_cents, would_profit, resolved.
    Returns pnl None when there is no usable current price and it is unresolved.
    """
    if resolved_yes is not None:
        current = settle_value_cents(side, resolved_yes)
        pnl = current - entry_cents
        return {"current_value_cents": round(current, 2), "pnl_cents": round(pnl, 2),
                "would_profit": pnl > 0, "resolved": True}
    if yes_bid is None or yes_ask is None or (yes_bid == 0 and yes_ask == 0):
        return {"current_value_cents": None, "pnl_cents": None,
                "would_profit": None, "resolved": False}
    current = mark_value_cents(side, yes_bid, yes_ask)
    pnl = current - entry_cents
    return {"current_value_cents": round(current, 2), "pnl_cents": round(pnl, 2),
            "would_profit": pnl > 0, "resolved": False}


# --- local JSONL store -------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


def record_recommendation(
    cycle_ts: str,
    ticker: str,
    side: str,
    entry_cents: float,
    predicted_prob: float,
    edge_cents: float,
    n_sources: int,
    sources: list[str],
    category: str = "",
    suggested_size_dollars: float | None = None,
) -> str:
    """Append one open recommendation to the local store. Returns its id."""
    rec_id = str(uuid.uuid4())
    _append_jsonl(_RECS_FILE, {
        "rec_id": rec_id,
        "cycle_ts": cycle_ts,
        "recorded_at": _now_iso(),
        "ticker": ticker,
        "side": side,
        "entry_price_cents": round(float(entry_cents), 2),
        "predicted_prob": round(float(predicted_prob), 4),
        "edge_cents": round(float(edge_cents), 2),
        "n_sources": n_sources,
        "sources": sources,
        "category": category,
        "suggested_size_dollars": suggested_size_dollars,
        "status": "open",
    })
    return rec_id


def load_recommendations() -> list[dict]:
    return _read_jsonl(_RECS_FILE)


def open_recommendations() -> list[dict]:
    return [r for r in load_recommendations() if r.get("status") == "open"]


def append_mark(rec_id: str, ticker: str, mark: dict) -> None:
    _append_jsonl(_MARKS_FILE, {"rec_id": rec_id, "ticker": ticker,
                                "checked_at": _now_iso(), **mark})


def close_recommendation(rec_id: str) -> None:
    """Rewrite the recs file marking a recommendation resolved (rare; small file)."""
    rows = load_recommendations()
    for row in rows:
        if row.get("rec_id") == rec_id:
            row["status"] = "resolved"
    _RECS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _RECS_FILE.write_text("\n".join(json.dumps(r, default=str) for r in rows) + ("\n" if rows else ""))


def _latest_marks() -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for mark in _read_jsonl(_MARKS_FILE):
        latest[mark["rec_id"]] = mark  # later marks overwrite — file is append-ordered
    return latest


def _scorecard(marks: list[dict]) -> dict:
    scored = [m for m in marks if m.get("pnl_cents") is not None]
    if not scored:
        return {"marked": 0, "wins": 0, "win_rate": None, "avg_pnl_cents": None}
    wins = sum(1 for m in scored if m.get("would_profit"))
    avg = sum(m["pnl_cents"] for m in scored) / len(scored)
    return {"marked": len(scored), "wins": wins,
            "win_rate": round(wins / len(scored), 3), "avg_pnl_cents": round(avg, 2)}


def performance_summary() -> dict:
    """Aggregate the latest mark per recommendation into a quick scorecard."""
    return _scorecard(list(_latest_marks().values()))


def performance_by_source() -> dict[str, dict]:
    """Scorecard sliced by each recommendation's signal sources.

    Lets us compare which signal — or which whale scorer (recorded as
    ``whale:<scorer>``) — actually predicts outcomes best.
    """
    latest = _latest_marks()
    recs = {r["rec_id"]: r for r in load_recommendations()}
    by_source: dict[str, list[dict]] = {}
    for rec_id, mark in latest.items():
        for source in (recs.get(rec_id, {}).get("sources") or ["unknown"]):
            by_source.setdefault(source, []).append(mark)
    return {source: _scorecard(marks) for source, marks in by_source.items()}
