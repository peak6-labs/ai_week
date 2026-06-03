"""Adapter between Kalshi's prod API schema and the codebase's canonical fields.

The prod API delivers prices as dollar strings (``yes_bid_dollars: "0.1000"``) and
sizes/volumes/open-interest as fixed-point strings (``volume_24h_fp: "559.03"``),
and wraps the book in ``orderbook_fp`` with ``yes_dollars``/``no_dollars`` levels.
The rest of the codebase reads canonical, cent-denominated names — ``yes_bid``,
``yes_ask``, ``no_bid``, ``no_ask``, ``last_price``, ``volume_24h``, ``volume``,
``open_interest`` — and an ``orderbook`` with ``yes``/``no`` ``[cents, size]``
levels. Normalize at the client boundary so no consumer reads a field that the
live API does not actually return.
"""
from __future__ import annotations

# prod dollar field -> canonical cent field
_DOLLAR_TO_CENTS = {
    "yes_bid_dollars": "yes_bid",
    "yes_ask_dollars": "yes_ask",
    "no_bid_dollars": "no_bid",
    "no_ask_dollars": "no_ask",
    "last_price_dollars": "last_price",
}
# prod fixed-point field -> canonical numeric field
_FP_TO_NUMERIC = {
    "volume_24h_fp": "volume_24h",
    "volume_fp": "volume",
    "open_interest_fp": "open_interest",
}


def _to_cents(raw) -> float | None:
    try:
        return float(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


def _to_float(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def normalize_market(market: dict) -> dict:
    """Add canonical cent/numeric fields to a prod market dict, in place.

    Idempotent and non-destructive: a canonical field that already holds a value
    (e.g. an old-schema snapshot row) is never overwritten, and absent source
    fields map to nothing rather than inventing a value.
    """
    if not isinstance(market, dict):
        return market
    for source_field, canonical in _DOLLAR_TO_CENTS.items():
        if market.get(canonical) is None and source_field in market:
            cents = _to_cents(market[source_field])
            if cents is not None:
                market[canonical] = cents
    for source_field, canonical in _FP_TO_NUMERIC.items():
        if market.get(canonical) is None and source_field in market:
            value = _to_float(market[source_field])
            if value is not None:
                market[canonical] = value
    return market


def _normalize_levels(levels) -> list:
    out = []
    for level in levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _to_cents(level[0])
        size = _to_float(level[1])
        if price is None or size is None:
            continue
        out.append([int(price), size])
    return out


def normalize_orderbook(response: dict) -> dict:
    """Add a canonical ``orderbook`` {yes, no: [[cents, size], ...]} to a response.

    Reads the prod ``orderbook_fp`` ({yes_dollars, no_dollars}) when present; if a
    canonical ``orderbook`` already exists, it is left untouched.
    """
    if not isinstance(response, dict) or response.get("orderbook") is not None:
        return response
    fp = response.get("orderbook_fp")
    if isinstance(fp, dict):
        response["orderbook"] = {
            "yes": _normalize_levels(fp.get("yes_dollars")),
            "no": _normalize_levels(fp.get("no_dollars")),
        }
    return response
