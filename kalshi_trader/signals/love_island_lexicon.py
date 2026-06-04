"""Curated Love Island franchise-catchphrase base rates for the mentions market.

The ``KXLOVEISLMENTION`` market asks whether the cast will *say* a given word in an
episode. GDELT (the generic mentions-signal corpus) has zero Love Island coverage,
and full episode transcripts are not available, so this is a hand-seeded prior:
franchise staples ("bombshell", "graft") are said most episodes; niche terms
("paralympic") almost never. The agent treats these as a starting probability and
adjusts up/down with the specific upcoming-episode teaser context — they are not a
substitute for a transcript corpus.

Base rates are rough per-episode P(said at least once), seeded from franchise
familiarity; refine them from the backtest as settled mentions markets accumulate.
"""
from __future__ import annotations

# Canonical phrase → (per-episode base rate, alias tokens matched case-insensitively
# as substrings against the contract's subtitle).
LOVE_ISLAND_CATCHPHRASE_PRIORS: dict[str, tuple[float, tuple[str, ...]]] = {
    "bombshell": (0.70, ("bombshell",)),
    "graft": (0.40, ("graft",)),
    "pull you for a chat": (0.55, ("pull you for a chat", "pull for a chat", "for a chat")),
    "firepit": (0.50, ("firepit", "fire pit")),
    "dumped": (0.45, ("dump", "dumped")),
    "loyalty": (0.50, ("loyal", "loyalty")),
    "drama": (0.45, ("drama",)),
    "date": (0.60, ("date", "dating")),
    "ick": (0.25, ("ick",)),
    "ex": (0.30, ("ex", "exes")),
    "mugged off": (0.40, ("mug", "mugged", "mugging", "mog", "mogged", "mogging")),
    "my type on paper": (0.30, ("type on paper", "on paper")),
    "casa amor": (0.10, ("casa amor", "casa")),
    "hideaway": (0.15, ("hideaway",)),
    "recoupling": (0.45, ("recoupl", "coupling")),
    "fiji": (0.20, ("fiji",)),
    "prize money": (0.15, ("prize", "100k", "100,000", "$100")),
    "peacock": (0.10, ("peacock",)),
    "social media": (0.20, ("instagram", "tiktok", "social media", " ig ")),
    "paralympic": (0.05, ("paralympic", "paralympian")),
}


def lookup_catchphrase_prior(phrase: str) -> dict[str, object]:
    """Look up the curated per-episode base rate for a mentions-market phrase.

    Matches the contract subtitle (which may list slash-separated variants, e.g.
    "Mog / Mogged / Mogging") against each canonical phrase's alias tokens. Returns
    the strongest match.

    Args:
        phrase: The contract's subtitle / phrase text.

    Returns:
        ``{"matched": bool, "canonical": str, "base_rate": float | None}``. When no
        franchise staple matches, ``matched`` is False and ``base_rate`` is None —
        the caller must treat that as "no prior", never as a low probability.
    """
    normalized = (phrase or "").lower()
    best_canonical = ""
    best_base_rate: float | None = None
    best_token_length = 0
    for canonical, (base_rate, alias_tokens) in LOVE_ISLAND_CATCHPHRASE_PRIORS.items():
        for alias in alias_tokens:
            if alias in normalized and len(alias) > best_token_length:
                best_canonical = canonical
                best_base_rate = base_rate
                best_token_length = len(alias)
    if best_base_rate is None:
        return {"matched": False, "canonical": "", "base_rate": None}
    return {"matched": True, "canonical": best_canonical, "base_rate": best_base_rate}
