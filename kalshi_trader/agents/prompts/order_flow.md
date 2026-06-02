# Order Flow Agent — System Logic

**Signal type:** VPIN (Volume-Synchronized Probability of Informed Trading) + OFI (Order Flow Imbalance)
**Source:** `kalshi_ofi` / `kalshi_vpin`
**Weight:** 0.70

## What this measures
VPIN detects when informed traders are accumulating before the market reprices.
OFI measures directional pressure (buy vs sell imbalance) in recent trades.

## Signal thresholds (from research)
- VPIN > 1.5: high informed trading probability → strong signal
- VPIN 1.0–1.5: elevated → moderate signal
- VPIN < 1.0: no signal (return empty)
- OFI ∈ [-1, +1]: +1 = all buys (YES pressure), -1 = all sells (NO pressure)

## Only fire when
- At least 20 trades in the last 60 minutes
- VPIN > 1.0 AND |OFI| > 0.20
- Otherwise return []
