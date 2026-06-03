#!/usr/bin/env python
"""Deterministic signal scorer — weights signals and computes edge.

All math is done here. Claude receives the output and does reasoning/judgment only.

Usage:
    python scripts/score_signals.py --signals-file /tmp/signals.json [--config runtime_config.json]

Input JSON format (per market) — either shape is accepted:

  Preferred (orchestrate pipeline): a flat list of agent SignalEstimate dicts.
    {
      "ticker": "MARKET-TICKER", "title": "...", "category": "...",
      "yes_ask": 35.0, "hours_to_close": 24.0,
      "signal_estimates": [
        {"source": "kalshi_bias", "probability": 0.62, "uncertainty": 0.05,
         "weight": 0.65, "data_issued_at": "2026-06-02T18:00:00Z", "metadata": {...}},
        ...
      ]
    }

  Legacy: a mapping of signal name to raw signal data, each re-derived by a
  score_* function.
    { ..., "signals": {"polymarket_price": {...raw gap data...}, ...} }

Output: JSON with weighted_probability, edge_cents, confidence, kelly_fraction per market.
"""
from __future__ import annotations

import json
import math
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def score_weather(raw: dict, cfg: dict) -> dict | None:
    """Convert raw NOAA data to probability estimate using scipy-style normal CDF."""
    if not raw or raw.get("temp_high") is None:
        return None

    metric = raw.get("metric", "")
    threshold = float(raw.get("threshold", 0))
    operator = raw.get("operator", "above")
    temp_high = raw.get("temp_high") or 85.0
    temp_low = raw.get("temp_low") or 65.0
    precip_pct = raw.get("precip_pct") or 0

    if metric in ("temp_high", "temp_low"):
        mean = (temp_high + temp_low) / 2.0
        std = max((temp_high - temp_low) / 4.0, 1.0)
        # Normal CDF approximation (no scipy needed)
        z = (threshold - mean) / std
        cdf = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        prob = 1.0 - cdf if operator == "above" else cdf
        uncertainty = float(cfg.get("uncertainty_noaa_temp", 0.08))
    elif metric == "precipitation":
        prob = precip_pct / 100.0
        uncertainty = float(cfg.get("uncertainty_noaa_precip", 0.05))
    else:
        return None

    return {
        "source": "noaa_gfs",
        "probability": round(max(0.01, min(0.99, prob)), 4),
        "uncertainty": uncertainty,
        "weight": float(cfg.get("weight_noaa", 0.85)),
        "data_age_minutes": raw.get("data_age_minutes", 0),
    }


def score_polymarket_price(raw: dict, cfg: dict) -> dict | None:
    """Convert raw Polymarket gap data to signal."""
    if not raw or "gap_cents" not in raw:
        return None

    gap = abs(float(raw["gap_cents"]))
    min_gap = float(cfg.get("poly_min_gap_cents", 10.0))
    if gap < min_gap:
        return None

    clob_mid = float(raw.get("clob_mid", 0.5))
    return {
        "source": "polymarket_price",
        "probability": round(clob_mid, 4),
        "uncertainty": float(cfg.get("uncertainty_poly_price", 0.03)),
        "weight": float(cfg.get("weight_polymarket_price", 0.75)),
        "gap_cents": round(float(raw["gap_cents"]), 2),
        "match_score": round(float(raw.get("match_score", 0)), 4),
    }


def score_polymarket_whale(raw: dict, cfg: dict) -> dict | None:
    """Convert raw whale entries to signal."""
    if not raw or not raw.get("whale_entries"):
        return None

    entries = raw["whale_entries"]
    yes_entries = [e for e in entries if e.get("side") == "YES"]
    no_entries = [e for e in entries if e.get("side") == "NO"]
    n = len(entries)

    total_size = sum(float(e.get("size_usd", 0)) for e in entries)
    yes_size = sum(float(e.get("size_usd", 0)) for e in yes_entries)
    prob = (yes_size / total_size) if total_size > 0 else 0.5

    distinct = len({e["wallet_address"] for e in entries})
    uncertainty_key = "uncertainty_whale_multi" if distinct > 1 else "uncertainty_whale_single"

    return {
        "source": "polymarket_whale",
        "probability": round(prob, 4),
        "uncertainty": float(cfg.get(uncertainty_key, 0.12)),
        "weight": float(cfg.get("weight_polymarket_whale", 0.6)),
        "whale_count": n,
        "distinct_wallets": distinct,
        "yes_count": len(yes_entries),
        "no_count": len(no_entries),
    }


def score_x(raw: dict, cfg: dict) -> dict | None:
    """Convert raw Grok response to signal."""
    if not raw or not raw.get("summary"):
        return None
    if float(raw.get("probability", 0.5)) == 0.5 and float(raw.get("uncertainty", 1.0)) >= 1.0:
        return None

    return {
        "source": "x_grok",
        "probability": round(float(raw.get("probability", 0.5)), 4),
        "uncertainty": round(float(raw.get("uncertainty", 0.3)), 4),
        "weight": float(cfg.get("weight_x_grok", 0.6)),
        "summary": raw.get("summary", ""),
        "velocity_24h": raw.get("velocity", {}).get("24h", 0),
    }


def score_order_flow(raw: dict, cfg: dict) -> dict | None:
    """Convert raw OFI/VPIN to signal."""
    if not raw or "ofi" not in raw:
        return None

    ofi = float(raw.get("ofi", 0))
    vpin = float(raw.get("vpin", 0.5))
    scale = float(cfg.get("ofi_prob_scale", 0.25))

    # OFI in [-1,1] → probability shift around 0.5
    prob = max(0.01, min(0.99, 0.5 + ofi * scale))
    # Uncertainty: lower when VPIN is high (informed trading) and OFI is strong
    uncertainty = max(0.08, 0.20 - abs(ofi) * 0.10 - max(0, vpin - 0.5) * 0.10)

    return {
        "source": "order_flow",
        "probability": round(prob, 4),
        "uncertainty": round(uncertainty, 4),
        "weight": float(cfg.get("weight_market_maker", 0.65)),
        "ofi": round(ofi, 4),
        "vpin": round(vpin, 4),
        "direction": raw.get("direction", "neutral"),
    }


def score_market_maker(raw: dict, cfg: dict) -> dict | None:
    """Convert raw orderbook data to signal."""
    if not raw or "spread_cents" not in raw:
        return None

    imbalance = float(raw.get("bid_ask_imbalance", 0))
    scale = float(cfg.get("mm_imbalance_prob_scale", 0.25))
    prob = max(0.01, min(0.99, 0.5 + imbalance * scale))

    return {
        "source": "market_maker",
        "probability": round(prob, 4),
        "uncertainty": 0.15,
        "weight": float(cfg.get("weight_market_maker", 0.65)),
        "spread_cents": raw.get("spread_cents"),
        "bid_ask_imbalance": round(imbalance, 4),
        "best_bid": raw.get("best_bid"),
        "best_ask": raw.get("best_ask"),
    }


def score_kalshi_bias(raw: dict, cfg: dict, yes_ask: float) -> dict | None:
    """Convert raw bias data to probability correction."""
    if not raw or raw.get("direction") == "none":
        return None

    direction = raw.get("direction", "none")
    magnitude = float(raw.get("magnitude_cents", 0))
    if magnitude < 1.0:
        return None

    # Bias correction: shift probability toward fair value
    base = yes_ask / 100.0
    if direction == "yes":
        prob = min(0.99, base + magnitude / 100.0)
    else:
        prob = max(0.01, base - magnitude / 100.0)

    return {
        "source": "kalshi_bias",
        "probability": round(prob, 4),
        "uncertainty": 0.05,
        "weight": 0.70,
        "bias_type": raw.get("bias_type", ""),
        "magnitude_cents": magnitude,
        "direction": direction,
        "reasoning": raw.get("reasoning", ""),
    }


# Lower bound on combined uncertainty after the agreement boost — keeps a tight
# corroboration from collapsing confidence to an unrealistic near-zero.
_AGREEMENT_UNCERTAINTY_FLOOR: float = 0.02


def combine_signals(signals: list[dict], cfg: dict) -> dict:
    """Staleness-discounted weighted average of signal probabilities."""
    if not signals:
        return {"combined_probability": 0.5, "uncertainty": 1.0, "n_sources": 0}

    total_w = 0.0
    w_prob = 0.0
    w_unc = 0.0

    for s in signals:
        age = float(s.get("data_age_minutes", 0))
        eff_w = float(s["weight"]) * math.exp(-age / 360.0)
        total_w += eff_w
        w_prob += eff_w * float(s["probability"])
        w_unc += eff_w * float(s["uncertainty"])

    if total_w == 0:
        return {"combined_probability": 0.5, "uncertainty": 1.0, "n_sources": 0}

    combined_prob = w_prob / total_w
    combined_unc = w_unc / total_w

    probs = [float(s["probability"]) for s in signals]
    spread = (max(probs) - min(probs)) if len(probs) > 1 else 0.0

    # Disagreement penalty
    if spread > 0.10:
        combined_unc += spread * 0.5

    # Independence-gated agreement boost: when >= 2 distinct source families
    # corroborate within a tight spread AND none is flagged non-independent,
    # tighten combined uncertainty (bounded by a floor). Probability is NOT
    # altered — the boost only sharpens confidence so a real edge clears the bar;
    # moving the probability would distort calibration. An NWS-office weather
    # authority sets independent_of_noaa False, so its agreement with noaa_gfs is
    # excluded (same model family — circular, not corroboration).
    if bool(cfg.get("agreement_boost_enabled", True)) and len(signals) >= 2:
        agreement_spread_threshold = float(cfg.get("agreement_spread_threshold", 0.03))
        agreement_uncertainty_factor = float(cfg.get("agreement_uncertainty_factor", 0.85))
        all_independent = all(bool(s.get("independent_of_noaa", True)) for s in signals)
        if spread <= agreement_spread_threshold and all_independent:
            combined_unc = max(
                _AGREEMENT_UNCERTAINTY_FLOOR, combined_unc * agreement_uncertainty_factor
            )

    return {
        "combined_probability": round(combined_prob, 4),
        "uncertainty": round(combined_unc, 4),
        "n_sources": len(signals),
        "sources": [s["source"] for s in signals],
    }


def compute_edge_and_kelly(
    combined_probability: float,
    yes_ask_cents: float,
    cfg: dict,
    yes_bid_cents: float | None = None,
) -> dict:
    """Compute fee-adjusted edge and half-Kelly fraction on the side we would take.

    Pure math. ``combined_probability`` is the fair YES probability.
    ``yes_ask_cents`` is the YES taker entry in cents. ``yes_bid_cents``, when
    available, is used to derive the real NO taker entry as ``100 - yes_bid``.
    If YES looks cheap we buy YES; if YES looks rich we buy NO. Edge and Kelly
    are always measured on the chosen side, so a NO-side mispricing (YES
    overpriced) surfaces symmetrically with a YES-side one rather than being
    silently dropped.
    """
    yes_price = yes_ask_cents / 100.0

    # Take the side with positive raw edge, then express the probability and
    # taker entry price on that side: YES buys at the ask, NO buys at 100-bid.
    if combined_probability >= yes_price:
        side = "yes"
        side_probability = combined_probability
        side_price = yes_price
    else:
        side = "no"
        side_probability = 1.0 - combined_probability
        side_price = (
            (100.0 - float(yes_bid_cents)) / 100.0
            if yes_bid_cents is not None
            else 1.0 - yes_price
        )

    edge_cents = side_probability * 100 - side_price * 100

    # Kalshi fee: 0.07 * price * (1 - price) * 100. Symmetric in price, so it is
    # the same whether we take YES or NO.
    fee = 0.07 * side_price * (1.0 - side_price) * 100
    fee_adjusted_edge = edge_cents - fee

    # Half-Kelly on the chosen side: f* = (p*b - q) / b  where b = (1/price - 1).
    if side_price <= 0 or side_price >= 1:
        kelly = 0.0
    else:
        yes_net_odds = (1.0 / side_price) - 1.0
        complement_probability = 1.0 - side_probability
        full_kelly_fraction = (side_probability * yes_net_odds - complement_probability) / yes_net_odds
        kelly = max(0.0, full_kelly_fraction * 0.5)  # half-Kelly

    # Configurable edge bar (default 5¢) so it can be tuned from the paper loop
    # without a code change (#25).
    min_edge_cents = float(cfg.get("min_edge_cents", 5.0))

    # High-entry-price guardrail (#14): a leg entered at ≥ the cap has a brutal
    # payoff asymmetry — e.g. buying NO at 93¢ risks 93¢ to make 7¢, so one loss
    # erases ~13 wins. The only resolved paper loss so far was exactly this (a 93¢
    # NO, −93¢). Block these regardless of nominal edge; the cap is configurable.
    entry_price_cents = side_price * 100.0
    max_entry_price_cents = float(cfg.get("max_entry_price_cents", 90.0))
    entry_price_blocked = entry_price_cents > max_entry_price_cents

    worth_trading = (fee_adjusted_edge > min_edge_cents) and not entry_price_blocked

    return {
        "edge_cents": round(edge_cents, 2),
        "fee_adjusted_edge": round(fee_adjusted_edge, 2),
        "worth_trading": worth_trading,
        "entry_price_cents": round(entry_price_cents, 2),
        "entry_price_blocked": entry_price_blocked,
        "kelly_fraction": round(kelly, 4),
        "side": side,
    }


def usable_estimates(estimates: list[dict]) -> list[dict]:
    """Filter raw agent SignalEstimate dicts down to the ones worth combining.

    The signal agents already emit calibrated estimates as
    ``{source, probability, uncertainty, weight, data_issued_at, metadata}`` —
    so they feed ``combine_signals`` directly, no per-signal re-derivation needed.
    Drops non-informative estimates (uncertainty ≥ 0.99, e.g. an X search that
    returned no posts and defaulted to prob=0.5/uncertainty=1.0) and anything
    missing the numeric fields. Derives ``data_age_minutes`` from
    ``data_issued_at`` so the staleness discount in ``combine_signals`` applies.
    """
    now = datetime.now(tz=timezone.utc)
    usable: list[dict] = []
    for estimate in estimates or []:
        try:
            probability = float(estimate["probability"])
            uncertainty = float(estimate["uncertainty"])
            weight = float(estimate["weight"])
        except (KeyError, TypeError, ValueError):
            continue
        if uncertainty >= 0.99:
            continue  # non-informative — agent found nothing usable
        # Empty-data guard: an estimate flagged as having no underlying data
        # (e.g. an X search that found zero posts) is absence-inferred, not
        # evidence. Drop it regardless of the uncertainty the agent stamped on
        # it — some agents emit a confident-looking value off no data.
        metadata = estimate.get("metadata") or {}
        if metadata.get("data_quality") == "empty" or metadata.get("post_count") == 0:
            continue
        issued = estimate.get("data_issued_at")
        age_minutes = 0.0
        if issued:
            try:
                issued_dt = datetime.fromisoformat(str(issued).replace("Z", "+00:00"))
                age_minutes = max(0.0, (now - issued_dt).total_seconds() / 60.0)
            except (ValueError, TypeError):
                age_minutes = 0.0
        usable.append({
            "source": estimate.get("source", "unknown"),
            "probability": probability,
            "uncertainty": uncertainty,
            "weight": weight,
            "data_age_minutes": age_minutes,
            # Independence flag for the agreement boost. Absent → independent.
            # An NWS-office weather authority stamps this False (same model
            # family as noaa_gfs, so its agreement is circular, not corroboration).
            "independent_of_noaa": metadata.get("independent_of_noaa", True),
        })
    return usable


# Source families where one upstream call emits several "slice" estimates that
# must NOT be counted as independent sources (they share a common input).
_SOURCE_FAMILY_PREFIXES: list[str] = ["x_grok"]


def _source_family(source: str) -> str:
    """Map a possibly-sliced source name to its family (e.g. x_grok_news → x_grok)."""
    for prefix in _SOURCE_FAMILY_PREFIXES:
        if source.startswith(prefix):
            return prefix
    return source


def collapse_source_families(estimates: list[dict]) -> list[dict]:
    """Collapse slices of one upstream call into a single source estimate.

    Several X (Grok) strategy slices — buzz/sentiment/experts/news — come from
    one Grok query, so counting them as 4 sources both inflates ``n_sources``
    (the actionability gate) and gives that one query 4x the weight in the
    combine. Average each family's slices into one estimate carrying a single
    representative weight, keyed by the family name.
    """
    by_family: dict[str, list[dict]] = {}
    order: list[str] = []
    for estimate in estimates:
        family = _source_family(estimate.get("source", "unknown"))
        if family not in by_family:
            by_family[family] = []
            order.append(family)
        by_family[family].append(estimate)

    collapsed: list[dict] = []
    for family in order:
        members = by_family[family]
        if len(members) == 1:
            collapsed.append({**members[0], "source": family})
            continue
        count = len(members)
        collapsed.append({
            "source": family,
            "probability": sum(float(m["probability"]) for m in members) / count,
            "uncertainty": sum(float(m["uncertainty"]) for m in members) / count,
            "weight": sum(float(m["weight"]) for m in members) / count,
            "data_age_minutes": min(float(m.get("data_age_minutes", 0)) for m in members),
            # The family is independent only if every slice is.
            "independent_of_noaa": all(m.get("independent_of_noaa", True) for m in members),
        })
    return collapsed


def score_market(market_data: dict, cfg: dict) -> dict:
    """Score one market deterministically. Returns full scored output.

    Two input shapes are supported per market:
    - ``signal_estimates``: a flat list of agent SignalEstimate dicts (the shape
      the orchestrate pipeline produces). Combined directly via ``usable_estimates``.
    - ``signals``: a mapping of signal name to raw signal data (legacy shape).
      Each is converted to an estimate by its ``score_*`` function.
    """
    yes_ask = float(market_data.get("yes_ask", 50.0))

    if market_data.get("signal_estimates") is not None:
        scored = usable_estimates(market_data["signal_estimates"])
    else:
        raw_signals = market_data.get("signals", {})
        scored = []
        scorers = {
            "weather":          lambda r: score_weather(r, cfg),
            "polymarket_price": lambda r: score_polymarket_price(r, cfg),
            "polymarket_whale": lambda r: score_polymarket_whale(r, cfg),
            "x":                lambda r: score_x(r, cfg),
            "order_flow":       lambda r: score_order_flow(r, cfg),
            "market_maker":     lambda r: score_market_maker(r, cfg),
            "kalshi_bias":      lambda r: score_kalshi_bias(r, cfg, yes_ask),
        }
        for name, fn in scorers.items():
            raw = raw_signals.get(name)
            if raw:
                result = fn(raw)
                if result:
                    scored.append(result)

    # Collapse multi-slice families (e.g. the four x_grok_* slices) into one
    # source so they neither inflate n_sources nor over-weight the combine.
    scored = collapse_source_families(scored)

    combined = combine_signals(scored, cfg)
    edge = compute_edge_and_kelly(
        combined["combined_probability"],
        yes_ask,
        cfg,
        yes_bid_cents=market_data.get("yes_bid"),
    )

    # With no usable signals the combined probability defaults to 0.5, which
    # against an extreme price (e.g. a 3¢ longshot) would fabricate a huge
    # "edge". A market with no signal is never actionable — force it off.
    if combined["n_sources"] == 0:
        edge = {**edge, "edge_cents": 0.0, "fee_adjusted_edge": 0.0,
                "worth_trading": False, "kelly_fraction": 0.0}

    return {
        "ticker": market_data.get("ticker", ""),
        "title": market_data.get("title", ""),
        "category": market_data.get("category", ""),
        "yes_ask": yes_ask,
        # Carry yes_bid through so downstream consumers can price the NO side
        # (NO entry = 100 - yes_bid). Defaults to yes_ask when the input omits it.
        "yes_bid": float(market_data.get("yes_bid", yes_ask)),
        "hours_to_close": market_data.get("hours_to_close", 0),
        "scored_signals": scored,
        **combined,
        **edge,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic signal scorer")
    parser.add_argument("--signals-file", required=True,
                        help="JSON file with markets and their raw signal data")
    parser.add_argument("--config", default="runtime_config.json",
                        help="Config file with signal weights")
    args = parser.parse_args()

    cfg = load_config(args.config)
    data = json.loads(Path(args.signals_file).read_text())

    # Support both single market dict and list of markets
    markets = data if isinstance(data, list) else [data]
    results = [score_market(m, cfg) for m in markets]
    results.sort(key=lambda r: abs(r.get("fee_adjusted_edge", 0)), reverse=True)

    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
