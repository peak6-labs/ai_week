"""Curated Love Island X (Twitter) accounts + hashtags for the X sentiment search.

This project does NOT use the X/Twitter API directly (its free tier is read-crippled,
and we hold no X account credentials — "following" is an outward action on a real
account). X signal comes from **Grok's `x_search`** tool. "Following these accounts"
therefore means *scoping/prioritizing the Grok search to them* — the same mechanism
the weather agent uses to restrict to named meteorologist authorities.

These are the highest-engagement LIUSA accounts as of early June 2026 (official hub +
the meme/reaction accounts that dominate the fandom). Handles are stored without the
leading "@". Grok's ``allowed_x_handles`` caps at 20; this list stays well under.
"""
from __future__ import annotations

# Highest-engagement Love Island USA accounts. @loveislandusa is the official hub
# (promos/teasers — strong signal for binary_event markets); the rest are the
# meme/reaction accounts that lead fandom engagement (sentiment for winner markets).
LOVE_ISLAND_X_HANDLES: list[str] = [
    "loveislandusa",   # official
    "snoozellle",
    "iTalkShxtt",
    "naomixsoleil",
    "tayliux",
    "EliteDaily",
    "kattzeye",
    "FlyGrlJustice",
    "roanrighoe",
]

LOVE_ISLAND_HASHTAGS: list[str] = ["#LoveIslandUSA", "#LoveIsland", "LIUSA"]


def love_island_x_query_focus() -> str:
    """A query suffix that focuses Grok's x_search on the right accounts + hashtags.

    Appended to the search query so Grok surfaces the Love-Island-tagged conversation
    and weights the high-engagement accounts, without hard-restricting (which would
    drop the broader fandom sentiment that public-vote markets depend on).
    """
    handles = ", ".join(f"@{handle}" for handle in LOVE_ISLAND_X_HANDLES)
    hashtags = " ".join(LOVE_ISLAND_HASHTAGS)
    return (
        f" Focus on Love Island posts tagged {hashtags}, prioritizing high-engagement "
        f"accounts like {handles}."
    )
