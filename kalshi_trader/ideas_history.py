"""Pure transform shared by the local (paper JSONL) and Supabase readers of the
Ideas History view.

No I/O. Given already-normalized recommendation dicts (each carrying ``rec_id``
and ``recorded_at``) and mark dicts (each carrying ``rec_id`` and ``checked_at``),
``join_recommendations_and_marks`` produces the exact shape the dashboard
consumes: one dict per recommendation (newest first by ``recorded_at``), each
carrying its original fields plus an ordered ``marks`` timeline. Keeping this in
one place means the two sources cannot drift.
"""

from __future__ import annotations

from datetime import datetime


def parse_iso_timestamp(value) -> datetime | None:
    """Parse an ISO timestamp string into an aware datetime; None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def join_recommendations_and_marks(
    recommendations: list[dict], marks: list[dict]
) -> list[dict]:
    """Join each recommendation with its ordered timeline of marks.

    Each output recommendation keeps all its original fields and gains a
    ``marks`` list. Every mark carries ``checked_at``, ``current_value_cents``,
    ``pnl_cents``, ``would_profit``, ``resolved`` and ``elapsed_seconds`` (seconds
    between the recommendation's ``recorded_at`` and the mark's ``checked_at``, or
    None when either timestamp is missing). Recommendations are sorted newest
    first by ``recorded_at``; marks within each are sorted oldest first by
    ``checked_at``.
    """
    marks_by_rec_id: dict[str, list[dict]] = {}
    for mark in marks:
        marks_by_rec_id.setdefault(mark.get("rec_id"), []).append(mark)

    joined: list[dict] = []
    for recommendation in recommendations:
        rec_id = recommendation.get("rec_id")
        recorded_at = parse_iso_timestamp(recommendation.get("recorded_at"))

        timeline: list[dict] = []
        for mark in marks_by_rec_id.get(rec_id, []):
            checked_at = parse_iso_timestamp(mark.get("checked_at"))
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
