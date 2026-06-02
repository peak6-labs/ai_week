#!/usr/bin/env python
"""Deterministic signal scorer — weights signals and computes edge.

All math is done here. Claude receives the output and does reasoning/judgment only.

Usage:
    python scripts/score_signals.py --signals-file /tmp/signals.json [--config runtime_config.json]

Input JSON format:
    {
      "ticker": "MARKET-TICKER",
      "title": "...",
      "category": "...",
      "yes_ask": 35.0,
      "hours_to_close": 24.0,
      "signals": {
        "weather":          {...raw NOAA data...},
        "polymarket_price": {...raw gap data...},
        "polymarket_whale": {...raw whale entries...},
        "x":               {...raw Grok response...},
        "order_flow":      {...raw OFI/VPIN...},
        "market_maker":    {...raw spread data...},
        "kalshi_bias":     {...raw bias data...}
      }
    }

Output: JSON with weighted_probability, edge_cents, confidence, kelly_fraction per market.
"""
from __future__ import annotations

import json
import math
import sys
import argparse
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

    # Disagreement penalty
    probs = [float(s["probability"]) for s in signals]
    if len(probs) > 1:
        spread = max(probs) - min(probs)
        if spread > 0.10:
            combined_unc += spread * 0.5

    return {
        "combined_probability": round(combined_prob, 4),
        "uncertainty": round(combined_unc, 4),
        "n_sources": len(signals),
        "sources": [s["source"] for s in signals],
    }


def compute_edge_and_kelly(combined_prob: float, yes_ask_cents: float, cfg: dict) -> dict:
    """Compute fee-adjusted edge and half-Kelly fraction. Pure math."""
    price = yes_ask_cents / 100.0
    edge_cents = combined_prob * 100 - yes_ask_cents

    # Kalshi fee: 0.07 * price * (1 - price) * 100
    fee = 0.07 * price * (1.0 - price) * 100
    fee_adjusted_edge = edge_cents - fee

    # Kelly: f* = (p*b - q) / b  where b = (1/price - 1)
    if price <= 0 or price >= 1:
        kelly = 0.0
    else:
        b = (1.0 / price) - 1.0
        q = 1.0 - combined_prob
        f_star = (combined_prob * b - q) / b
        kelly = max(0.0, f_star * 0.5)  # half-Kelly

    return {
        "edge_cents": round(edge_cents, 2),
        "fee_adjusted_edge": round(fee_adjusted_edge, 2),
        "worth_trading": fee_adjusted_edge > 5.0,
        "kelly_fraction": round(kelly, 4),
        "side": "yes" if combined_prob > price else "no",
    }


def score_market(market_data: dict, cfg: dict) -> dict:
    """Score one market deterministically. Returns full scored output."""
    raw_signals = market_data.get("signals", {})
    yes_ask = float(market_data.get("yes_ask", 50.0))

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

    combined = combine_signals(scored, cfg)
    edge = compute_edge_and_kelly(combined["combined_probability"], yes_ask, cfg)

    return {
        "ticker": market_data.get("ticker", ""),
        "title": market_data.get("title", ""),
        "category": market_data.get("category", ""),
        "yes_ask": yes_ask,
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
