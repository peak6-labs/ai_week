from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_trader.models import SignalEstimate
from kalshi_trader.agents.base import BaseAgent
from kalshi_trader.agents.parsing import parse_signal_estimates, estimate_to_dict

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_POLITICAL_KEYWORDS = {
    "election", "vote", "president", "senate", "congress", "governor",
    "mayor", "poll", "approval", "democrat", "republican", "party",
    "candidate", "primary", "ballot", "politics", "political",
}


def _is_political(category: str, title: str) -> bool:
    text = (category + " " + title).lower()
    return any(kw in text for kw in _POLITICAL_KEYWORDS)


def _horizon_factor(hours_to_resolution: float) -> float:
    """Scale down bias correction as market approaches resolution."""
    if hours_to_resolution < 12:
        return 0.30
    elif hours_to_resolution < 48:
        return 0.60
    else:
        return 1.0


def compute_bias_adjustment(
    price_prob: float,
    is_political: bool,
    hours_to_resolution: float,
) -> float:
    """Compute calibration bias adjustment.

    Returns the adjusted probability. If the adjustment is too small to trade
    (< 5 percentage points after horizon scaling), returns price_prob unchanged
    to signal 'no edge'.

    Args:
        price_prob: Market implied probability in [0, 1].
        is_political: Whether this is a political market.
        hours_to_resolution: Hours until market closes.

    Returns:
        Adjusted probability in [0, 1].
    """
    h = _horizon_factor(hours_to_resolution)

    if is_political:
        # Political underconfidence: push away from 0.5
        if price_prob > 0.5:
            raw_adj = 0.065 * h  # push toward YES
        elif price_prob < 0.5:
            raw_adj = -0.065 * h  # push toward NO
        else:
            raw_adj = 0.0
    else:
        # Favorite-longshot bias
        if price_prob < 0.15:
            # Longshot overpriced: true_prob ≈ market × 0.65
            raw_adj = price_prob * (0.65 - 1.0) * h  # negative: push down
        elif price_prob > 0.85:
            # Favorite underpriced: true_prob ≈ 1 - (1-p)*0.65
            raw_adj = (1.0 - price_prob) * (1.0 - 0.65) * h  # positive: push up
        else:
            raw_adj = 0.0

    adjusted = price_prob + raw_adj
    adjusted = max(0.01, min(0.99, adjusted))

    # Only return adjusted value if the move is meaningful (> 1pp)
    if abs(adjusted - price_prob) < 0.01:
        return price_prob  # signal "no edge"

    return adjusted


_SCHEMAS: list[dict] = [
    {
        "name": "apply_bias_corrections",
        "description": (
            "Apply Kalshi calibration bias corrections to a market price. "
            "Returns corrected_prob, raw_prob, corrections_applied, and delta_cents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "price_cents": {"type": "number", "description": "Current market midpoint in cents (0-100)"},
                "category": {"type": "string", "description": "Market category, e.g. 'politics'"},
            },
            "required": ["ticker", "price_cents", "category"],
        },
    },
    {
        "name": "build_bias_signal",
        "description": "Construct a SignalEstimate dict from the correction result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "correction_result": {
                    "type": "object",
                    "description": "The dict returned by apply_bias_corrections",
                },
            },
            "required": ["ticker", "correction_result"],
        },
    },
]


class KalshiBiasAgent:
    """Applies favorite-longshot and political underconfidence bias corrections via Claude tool-use loop."""

    def __init__(self, client: Any) -> None:
        self._client = client
        system_prompt = (_PROMPTS_DIR / "kalshi_bias.md").read_text()
        self._agent = BaseAgent(
            tools=_SCHEMAS,
            handlers={
                "apply_bias_corrections": self._apply_bias_corrections,
                "build_bias_signal": self._build_bias_signal,
            },
            system_prompt=system_prompt,
        )

    async def run(
        self,
        ticker: str,
        title: str,
        category: str = "",
        hours_to_resolution: float = 72.0,
    ) -> list[SignalEstimate]:
        # Fetch current price to include in prompt
        try:
            market_data = await self._client.get_market(ticker)
            market = market_data.get("market", market_data)
            yes_bid = market.get("yes_bid", 0) or 0
            yes_ask = market.get("yes_ask", 0) or 0
            price_cents = (yes_bid + yes_ask) / 2.0
        except Exception:
            return []

        if price_cents == 0:
            return []

        prompt = (
            f"Analyze calibration bias for this Kalshi market:\n"
            f"ticker: {ticker}\ntitle: {title}\n"
            f"category: {category}\nprice_cents: {price_cents:.1f}\n"
            f"hours_to_resolution: {hours_to_resolution}"
        )
        raw = await self._agent.run(prompt)
        return parse_signal_estimates(raw)

    async def _apply_bias_corrections(
        self, ticker: str, price_cents: float, category: str
    ) -> dict:
        """Apply calibration bias corrections to a market price."""
        raw_prob = price_cents / 100.0
        corrected = raw_prob
        corrections_applied: list[str] = []

        # Longshot bias: price < 20¢ → corrected = price × 0.72
        if raw_prob < 0.20:
            corrected = corrected * 0.72
            corrections_applied.append("longshot_bias")

        # Political underconfidence
        if "politic" in category.lower() or category.lower() == "politics":
            if raw_prob > 0.55:
                corrected = corrected * 1.08
                corrections_applied.append("political_underconfidence")
            elif raw_prob < 0.45:
                corrected = corrected * 0.92
                corrections_applied.append("political_underconfidence")

        # Near-certainty compression: price > 85¢ → corrected = price × 1.04
        if raw_prob > 0.85:
            corrected = corrected * 1.04
            corrections_applied.append("near_certainty")

        corrected = max(0.01, min(0.99, corrected))
        delta_cents = (corrected - raw_prob) * 100.0

        return {
            "corrected_prob": round(corrected, 6),
            "raw_prob": round(raw_prob, 6),
            "corrections_applied": corrections_applied,
            "delta_cents": round(delta_cents, 4),
        }

    async def _build_bias_signal(self, ticker: str, correction_result: dict) -> dict:
        """Construct a SignalEstimate from bias correction results."""
        corrected_prob = correction_result["corrected_prob"]
        raw_prob = correction_result["raw_prob"]
        corrections_applied = correction_result.get("corrections_applied", [])
        delta_cents = correction_result.get("delta_cents", 0.0)

        # Build a human-readable narrative
        bias_label = corrections_applied[0] if corrections_applied else "bias"
        narrative = (
            f"Bias correction applied ({', '.join(corrections_applied)}): "
            f"{raw_prob*100:.1f}¢ market → corrected probability {corrected_prob*100:.1f}¢. "
            f"Delta: {delta_cents:+.2f}¢."
        )

        estimate = SignalEstimate(
            source="kalshi_bias",
            probability=round(corrected_prob, 4),
            uncertainty=0.02,
            weight=0.55,
            data_issued_at=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "narrative": narrative,
                "data_quality": "fresh",
                "raw_prob": raw_prob,
                "corrected_prob": corrected_prob,
                "delta_cents": delta_cents,
                "corrections_applied": corrections_applied,
                # Legacy fields for backward-compat with existing tests
                "market_price_cents": raw_prob * 100.0,
                "adjusted_prob": round(corrected_prob, 4),
                "bias_type": bias_label,
                "is_political": "political_underconfidence" in corrections_applied,
            },
        )
        return estimate_to_dict(estimate)
