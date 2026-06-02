"""Utility functions for parsing SignalEstimate objects from agent output."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from kalshi_trader.models import SignalEstimate


def parse_signal_estimates(raw: str) -> list[SignalEstimate]:
    """Extract SignalEstimate objects from a fenced JSON block in raw text.

    Looks for ```json ... ``` block, parses the JSON array, and converts
    each item to a SignalEstimate. Malformed items are silently skipped.

    Args:
        raw: Raw string from agent output, expected to contain a JSON block.

    Returns:
        List of SignalEstimate objects; empty list on any parse failure.
    """
    match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    if not match:
        return []

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []

    results: list[SignalEstimate] = []
    for item in data:
        try:
            issued_raw = item["data_issued_at"]
            if isinstance(issued_raw, datetime):
                data_issued_at = issued_raw
                if data_issued_at.tzinfo is None:
                    data_issued_at = data_issued_at.replace(tzinfo=timezone.utc)
            else:
                data_issued_at = datetime.fromisoformat(str(issued_raw))
                if data_issued_at.tzinfo is None:
                    data_issued_at = data_issued_at.replace(tzinfo=timezone.utc)

            sig = SignalEstimate(
                source=str(item["source"]),
                probability=float(item["probability"]),
                uncertainty=float(item["uncertainty"]),
                weight=float(item["weight"]),
                data_issued_at=data_issued_at,
                metadata=item.get("metadata", {}),
            )
            results.append(sig)
        except (KeyError, ValueError, TypeError):
            continue

    return results


def estimate_to_dict(e: SignalEstimate) -> dict[str, Any]:
    """Serialize a SignalEstimate to a JSON-compatible dict.

    Args:
        e: SignalEstimate to serialize.

    Returns:
        Dict with source, probability, uncertainty, weight,
        data_issued_at (ISO string), and metadata.
    """
    return {
        "source": e.source,
        "probability": e.probability,
        "uncertainty": e.uncertainty,
        "weight": e.weight,
        "data_issued_at": e.data_issued_at.isoformat(),
        "metadata": e.metadata,
    }
