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
    disposition: str = "candidate",
) -> str:
    """Append one open recommendation to the local store. Returns its id.

    ``status`` is the *lifecycle* (open → resolved). ``disposition`` is the
    orthogonal *classification* the pipeline assigned at record time —
    ``approved`` (risk passed it onto the slate), ``worth_trading`` (cleared the
    edge+source bar but risk/challenge dropped it), or ``insufficient_edge``
    (scored but below the fee-adjusted edge bar). Recording every disposition,
    not just approved ones, lets us mark them all to market and check whether the
    edge bar is set at the right level — see ``performance_by_edge_bucket``.
    """
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
        "disposition": disposition,
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


def recommendations_with_marks() -> list[dict]:
    """Join every recommendation with its full ordered timeline of marks.

    Returns one dict per recommendation (newest first by ``recorded_at``),
    each carrying the original recommendation fields plus a ``marks`` list:
    the chronologically-ordered mark snapshots for that ``rec_id``, each with an
    ``elapsed_seconds`` field giving the time between the recommendation's
    ``recorded_at`` and the mark's ``checked_at`` so the UI can show intervals
    (e.g. +0h / +7h / +resolved). Read-only; never executes anything.
    """
    recommendations = load_recommendations()

    marks_by_rec_id: dict[str, list[dict]] = {}
    for mark in _read_jsonl(_MARKS_FILE):
        marks_by_rec_id.setdefault(mark.get("rec_id"), []).append(mark)

    joined: list[dict] = []
    for recommendation in recommendations:
        rec_id = recommendation.get("rec_id")
        recorded_at = _parse_iso(recommendation.get("recorded_at"))

        timeline: list[dict] = []
        for mark in marks_by_rec_id.get(rec_id, []):
            checked_at = _parse_iso(mark.get("checked_at"))
            elapsed_seconds: float | None = None
            if recorded_at is not None and checked_at is not None:
                elapsed_seconds = (checked_at - recorded_at).total_seconds()
            timeline.append({
                "checked_at": mark.get("checked_at"),
                "current_value_cents": mark.get("current_value_cents"),
                "pnl_cents": mark.get("pnl_cents"),
                "would_profit": mark.get("would_profit"),
                "resolved": mark.get("resolved"),
                "elapsed_seconds": elapsed_seconds,
            })
        timeline.sort(key=lambda mark: mark["checked_at"] or "")

        joined.append({**recommendation, "marks": timeline})

    joined.sort(key=lambda recommendation: recommendation.get("recorded_at") or "", reverse=True)
    return joined


def _parse_iso(value) -> datetime | None:
    """Parse an ISO timestamp string into an aware datetime; None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


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


def performance_by_disposition() -> dict[str, dict]:
    """Scorecard sliced by each recommendation's disposition.

    Compares how approved trades fared against the worth_trading-but-dropped and
    insufficient-edge candidates we recorded for backtest. If the rejected
    buckets win at a similar rate, our filters are leaving money on the table.
    """
    latest = _latest_marks()
    recs = {r["rec_id"]: r for r in load_recommendations()}
    by_disposition: dict[str, list[dict]] = {}
    for rec_id, mark in latest.items():
        disposition = recs.get(rec_id, {}).get("disposition") or "unknown"
        by_disposition.setdefault(disposition, []).append(mark)
    return {disposition: _scorecard(marks) for disposition, marks in by_disposition.items()}


# Fee-adjusted edge buckets in cents. The 5.0 boundary is the current
# worth_trading bar — splitting at it lets us read win-rate/avg-P&L on either
# side and judge whether the bar belongs higher or lower.
_EDGE_BUCKET_EDGES: list[float] = [0.0, 2.5, 5.0, 7.5, 10.0]


def _edge_bucket_label(edge_cents: float) -> str:
    if edge_cents < _EDGE_BUCKET_EDGES[0]:
        return "(-inf,0)"
    for low, high in zip(_EDGE_BUCKET_EDGES, _EDGE_BUCKET_EDGES[1:]):
        if low <= edge_cents < high:
            return f"[{low:g},{high:g})"
    return f"[{_EDGE_BUCKET_EDGES[-1]:g},inf)"


def performance_by_edge_bucket() -> dict[str, dict]:
    """Scorecard bucketed by the recommendation's edge_cents at record time.

    This is the calibration test for the edge threshold: each bucket's realized
    win-rate and avg P&L show whether the would-be profit actually rises with
    predicted edge, and where it crosses break-even.
    """
    latest = _latest_marks()
    recs = {r["rec_id"]: r for r in load_recommendations()}
    by_bucket: dict[str, list[dict]] = {}
    for rec_id, mark in latest.items():
        edge_cents = recs.get(rec_id, {}).get("edge_cents")
        if edge_cents is None:
            continue
        by_bucket.setdefault(_edge_bucket_label(float(edge_cents)), []).append(mark)
    return {bucket: _scorecard(marks) for bucket, marks in by_bucket.items()}
