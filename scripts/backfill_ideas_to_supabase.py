"""One-time backfill of the local paper-trade Ideas History into Supabase.

The write path mirrors recommendations + marks to Supabase best-effort, but
historically most never landed, so the shared tables lag the local JSONL store.
Now that the dashboard reads Supabase first, the shared tables must hold the full
history. This script upserts every local recommendation (idempotent by id, with
the true ``recorded_at`` as ``created_at``) and inserts each recommendation's
marks — skipping any recommendation Supabase has already marked, so re-running is
safe and never duplicates marks.

Read-only against Kalshi; writes only to the designated Supabase project. No
trade execution. Run:

    .venv/bin/python scripts/backfill_ideas_to_supabase.py
"""

from __future__ import annotations

import asyncio

from kalshi_trader import db, paper


async def main() -> None:
    local_ideas = paper.recommendations_with_marks()
    print(f"local store: {len(local_ideas)} recommendations")

    existing = await db.recommendations_with_marks()
    existing_rec_ids = {idea["rec_id"] for idea in existing}
    already_marked_rec_ids = {idea["rec_id"] for idea in existing if idea.get("marks")}
    print(f"supabase before: {len(existing)} recommendations, "
          f"{len(already_marked_rec_ids)} with marks")

    recommendations_written = 0
    marks_written = 0
    for idea in local_ideas:
        rec_id = idea["rec_id"]

        # insert_recommendation ignores the extra ``marks`` key; it upserts by id
        # and writes created_at from recorded_at (timestamp fidelity).
        await db.insert_recommendation(idea)
        if idea.get("status") == "resolved":
            await db.resolve_recommendation(rec_id)
        recommendations_written += 1

        # Only insert marks for recommendations Supabase has not already marked,
        # so re-running this backfill cannot duplicate mark rows.
        if rec_id not in already_marked_rec_ids:
            for mark in idea.get("marks", []):
                await db.insert_recommendation_mark(rec_id, mark)
                marks_written += 1

    print(f"upserted {recommendations_written} recommendations, "
          f"inserted {marks_written} marks")

    after = await db.recommendations_with_marks()
    after_marked = sum(1 for idea in after if idea.get("marks"))
    new_rec_ids = {idea["rec_id"] for idea in after} - existing_rec_ids
    print(f"supabase after: {len(after)} recommendations "
          f"(+{len(new_rec_ids)} new), {after_marked} with marks")


if __name__ == "__main__":
    asyncio.run(main())
