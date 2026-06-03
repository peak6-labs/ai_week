"""Format contract settlement terms into a uniform agent prompt block.

The signal pipeline's edge dies quietly when an agent measures the *wrong*
quantity: forecasting NYC temperature off NOAA when the contract settles on
AccuWeather, or counting a phrase said in Q&A when the market only counts
prepared remarks. This helper turns the structured settlement context fetched in
:mod:`kalshi_trader.contract_terms` (merged onto ``rules_primary`` by
``scripts/market_rules.py``) into one block that is appended to every
settlement-sensitive agent's prompt, carrying a single shared instruction:
measure the same thing, on the same source/criterion, that the contract settles
on — otherwise abstain or down-weight and say why.

The block is domain-agnostic by design: each agent (weather, mentions, polls,
cross-venue) applies the same instruction within its own field.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

# The one shared instruction every settlement-sensitive agent receives. Phrased
# generically so it lands the same way for a forecast, a base rate, a poll, or a
# cross-venue price comparison.
SETTLEMENT_INSTRUCTION = (
    "Measure the same quantity, on the same source and resolution criterion, that "
    "this contract settles on. If your data source or criterion differs from the "
    "settlement terms above — a different provider, station/coordinates, exact "
    "phrase or speaking context, race or resolution date, or threshold strictness "
    "(strict > vs >=) — then abstain or down-weight your signal and say why in the "
    "narrative."
)

# (cache/series field, human label) pairs rendered into the block, in order.
_RULE_FIELDS = (
    ("rules_primary", "Primary rule"),
    ("rules_secondary", "Secondary rule"),
    ("subtitle", "Subtitle"),
)


def parse_settlement_arg(raw: str | None) -> dict[str, Any] | None:
    """Parse a ``--settlement-json`` CLI argument into a dict (or None).

    Tolerant by design: missing/blank/malformed/non-object input returns None so
    a caller can simply skip the settlement block rather than crash.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _render_sources(settlement_sources: Any) -> str:
    """Render ``[{name, url}]`` into a compact, human-readable list."""
    rendered: list[str] = []
    for source in settlement_sources or []:
        if isinstance(source, Mapping):
            name = str(source.get("name") or "").strip()
            url = str(source.get("url") or "").strip()
        else:
            name, url = str(source).strip(), ""
        if not name:
            continue
        rendered.append(f"{name} ({url})" if url else name)
    return "; ".join(rendered)


def format_settlement_block(settlement: Mapping[str, Any] | None) -> str:
    """Format settlement context into a prompt block, or "" if there's nothing.

    Accepts the merged dict produced by ``market_rules.py``
    (``{rules_primary, rules_secondary, subtitle, settlement_sources,
    contract_terms_url}``) and renders the fields it finds, followed by the
    shared :data:`SETTLEMENT_INSTRUCTION`. Returns an empty string when no usable
    field is present, so callers can append unconditionally.
    """
    if not settlement:
        return ""

    lines: list[str] = []
    sources_text = _render_sources(settlement.get("settlement_sources"))
    if sources_text:
        lines.append(f"Settlement source(s): {sources_text}")
    for field, label in _RULE_FIELDS:
        text = settlement.get(field)
        if text:
            lines.append(f"{label}: {str(text).strip()}")
    terms_url = settlement.get("contract_terms_url")
    if terms_url:
        lines.append(f"Full contract-terms document: {terms_url}")

    if not lines:
        return ""

    body = "\n".join(lines)
    return f"## Contract settlement terms\n{body}\n\n{SETTLEMENT_INSTRUCTION}"


def settlement_metadata(settlement: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compact settlement basis to record on a deterministic signal's metadata.

    Deterministic pipelines (mentions, polls) cannot reason about the settlement
    terms the way an LLM agent can, but they can record what the contract settles
    on so the downstream adversarial check sees the basis the signal was built
    against. Returns ``{}`` when there is nothing worth recording.
    """
    if not settlement:
        return {}
    basis: dict[str, Any] = {}
    settlement_sources = settlement.get("settlement_sources")
    if settlement_sources:
        basis["settlement_sources"] = settlement_sources
    terms_url = settlement.get("contract_terms_url")
    if terms_url:
        basis["contract_terms_url"] = terms_url
    return basis
