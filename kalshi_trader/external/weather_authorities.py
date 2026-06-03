"""City → reputable-meteorologist X/Twitter handle map.

Curated from ``docs/knowledge/weather-authorities-by-city.md``. Used by the
weather pipeline to poll a *second*, ideally independent, forecast source
alongside NOAA so a weather market can produce more than one ``SignalEstimate``
and clear the orchestrator's ``min_agents >= 2`` actionability gate.

Keys are the **same canonical city strings** as ``CITY_COORDS`` in
``kalshi_trader.external.weather_parser`` — the strings ``parse_title`` returns —
so a parsed market's ``city`` field looks up directly here. Handles are stored
without the leading ``@``.

INDEPENDENCE WARNING
--------------------
``NWS_OFFICE_HANDLES`` lists the handles that are National Weather Service
offices. An NWS office forecast derives from the same model family as
``noaa_gfs``, so its agreement with NOAA is circular, not corroboration. Those
handles are flagged non-independent (see ``is_independent_authority``) and must
not earn the scorer's agreement boost.

Several NWS office handles below were **backfilled best-effort** for cities the
knowledge file lists only by station/office name (no explicit handle). They are
unverified guesses at the office's real handle. An invalid handle simply returns
no posts from Grok, so the pipeline fails safe to NOAA-only — but verify a handle
on x.com before relying on its forecasts.
"""
from __future__ import annotations

# Handles that are NWS offices (not independent of the NOAA model family).
# The handles below the divider are backfilled best-effort and UNVERIFIED.
NWS_OFFICE_HANDLES: set[str] = {
    # Verified-style handles for cities the knowledge file lists by NWS office.
    "NWSBoston",
    "NWSChicago",
    "NWSVegas",
    "NWSNewYorkNY",
    # --- Best-effort backfill (UNVERIFIED — confirm on x.com before trusting) ---
    "NWSLosAngeles",
    "NWSBoulder",       # NWS Denver/Boulder CO
    "NWSPhoenix",
    "NWSSeattle",
    "NWS_MountHolly",   # NWS Philadelphia/Mount Holly
    "NWSTwinCities",    # NWS Minneapolis/Twin Cities
    "NWSSanAntonio",    # NWS Austin/San Antonio
    "NWSBayArea",       # NWS San Francisco Bay Area
}

# City → ordered list of authority handles (most reputable first). Keys MUST be
# present in CITY_COORDS. Broadcast meteorologists are preferred where the
# knowledge file names them; otherwise the NWS office handle is the only option.
WEATHER_AUTHORITIES: dict[str, list[str]] = {
    # Broadcast meteorologists (independent of the NOAA model).
    "atlanta": ["BradNitzWSB", "JoanneFOX5"],
    "austin": ["averytomascowx", "nickbannin"],
    "dallas": ["wfaaweather"],
    "houston": ["mattlanza", "SpaceCityWX"],
    "miami": ["MichaelRLowry"],
    "oklahoma city": ["JackGerfenWX"],
    "washington": ["capitalweather"],
    # NWS-office-only cities (non-independent — flagged via NWS_OFFICE_HANDLES).
    "boston": ["NWSBoston"],
    "chicago": ["NWSChicago"],
    "las vegas": ["NWSVegas"],
    "new york": ["NWSNewYorkNY"],
    # Best-effort NWS backfill for cities the file lists only by station/office.
    "los angeles": ["NWSLosAngeles"],
    "denver": ["NWSBoulder"],
    "phoenix": ["NWSPhoenix"],
    "seattle": ["NWSSeattle"],
    "philadelphia": ["NWS_MountHolly"],
    "minneapolis": ["NWSTwinCities"],
    "san antonio": ["NWSSanAntonio"],
    "san francisco": ["NWSBayArea"],
}

# Canonical-city aliases → the WEATHER_AUTHORITIES key. CITY_COORDS holds three
# separate NYC keys; collapse them to the single "new york" authority list.
_CITY_ALIASES: dict[str, str] = {
    "nyc": "new york",
    "new york city": "new york",
}

# Lowercased NWS office handles for case-insensitive independence checks.
_NWS_OFFICE_HANDLES_LOWER: set[str] = {handle.lower() for handle in NWS_OFFICE_HANDLES}


def get_authorities(city: str) -> list[str]:
    """Return the authority handles for a canonical city string.

    Canonicalizes NYC aliases (``nyc`` / ``new york city`` / ``new york``) to one
    list. Returns an empty list for an unmapped city.
    """
    if not city:
        return []
    canonical_city = city.strip().lower()
    canonical_city = _CITY_ALIASES.get(canonical_city, canonical_city)
    return list(WEATHER_AUTHORITIES.get(canonical_city, []))


def is_independent_authority(handles: list[str]) -> bool:
    """True if at least one handle is a genuine broadcast met (not an NWS office).

    An NWS-office handle derives from the same model family as ``noaa_gfs``, so a
    list containing only NWS offices is *not* independent of NOAA.
    """
    return any(handle.lower() not in _NWS_OFFICE_HANDLES_LOWER for handle in handles)
