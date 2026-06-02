# Polymarket Microstructure Research — Synthesis

*Compiled 2026-06-02 from 5 academic papers. All findings are directly relevant to building signals from the poly_data on-chain CSV.*

---

## Paper Index

| Paper | arXiv | Key Contribution |
|---|---|---|
| Fill-Side Non-Retail Trading on Polymarket | [2605.11640](https://arxiv.org/html/2605.11640) | Behavioral tier framework; OI/VPIN/ILS metrics |
| Anatomy of a Decentralized Prediction Market | [2604.24366](https://arxiv.org/html/2604.24366v1) | Kyle's λ computation; spread/depth anatomy |
| Kyle's λ under Noise-Perturbed Order Flow | [2605.15746](https://arxiv.org/html/2605.15746) | Why textbook Kyle is wrong for on-chain data |
| From Iran to Taylor Swift: Informed Trading | [Harvard Law](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/) | 5-signal whale screen; 69.9% win rate at scale |
| Polymarket in the 2024 US Election | [2603.03136](https://arxiv.org/html/2603.03136) | Market maturation signals; arbitrage half-life |

---

## 1. Trader Tier Classification (Implementable Now)

From **Fill-Side Non-Retail Trading** — thresholds derived from on-chain fill data, directly applicable to our `trades.csv`:

| Tier | Criteria | Population | Notional Share |
|---|---|---|---|
| **Whale** | Total notional ≥ $1M | 68 addresses (0.1%) | **81.4%** |
| **HFO** (High-Freq Operator) | Fill intensity ≥ p95 AND market breadth ≥ p75 | 3,952 (3.8%) | — |
| **Power Trader** | Avg notional ≥ p75 AND total notional ≥ p75 | 6,738 (8.7%) | — |
| **Retail** | Notional < $10K | 82.1% of addresses | 6.8% |

**Retail per-fill avg: ~$4.77** — over 200× below synthetic-trader assumptions. Use this to flag anomalous small-size high-frequency behavior (possible informed response).

**6 on-chain-constructable features per address:**
1. Log fill intensity (fills per market per address)
2. Log avg notional per fill
3. Directional ratio (buy fills / total fills)
4. Market HHI (concentration across markets)
5. Intraday entropy (temporal distribution)
6. Log market breadth (unique markets)

**What is NOT available from on-chain data:** quote posting, quote cancellation, two-sided quoting behavior. Off-chain CLOB architecture means only fills are observable.

---

## 2. Market-Level Microstructure Signals

### Order Flow Signals (compute from `trades.csv`)

**Order Imbalance (OI):**
```
OI = (buy_volume_usd - sell_volume_usd) / (buy_volume_usd + sell_volume_usd)
```
Range: [-1, +1]. Values sustained above ±0.6 across consecutive windows signal directional informed flow.

**VPIN (Volume-Synchronized Probability of Informed Trading):**
- Divide trading volume into equal-size buckets (e.g., $1,000 USD each)
- Per bucket, classify trades as buy/sell from `maker_direction`/`taker_direction`
- VPIN = avg |buy_bucket_vol - sell_bucket_vol| / bucket_size over trailing 50 buckets
- High VPIN → elevated adverse selection → imminent price move

**ILS (Information Leakage Score):**
- Resolution-anchored: compare price trajectory in the N days before resolution to a null of no information leakage
- Flag markets where prices moved significantly toward resolution 24–48h before the event

**Kyle's λ (Price Impact):**
```python
# Per market, rolling window of 50 trades
import numpy as np
signed_vol = np.where(taker_direction == 'BUY', usd_amount, -usd_amount)
delta_price = price.diff()
lambda_ = np.cov(delta_price, signed_vol)[0,1] / np.var(signed_vol)
```

**Critical warning from [2605.15746]:** On-chain λ underestimates true informed-trader profits due to privacy noise. Do not use textbook calibration. Use λ as a relative (not absolute) measure — spikes vs. trailing median matter more than absolute value.

**From [2604.24366]:** Feed-inferred trade direction is only ~59% accurate. **Must use on-chain `maker_direction`/`taker_direction` from `trades.csv`** for valid microstructure computation.

### Spread & Depth Signals

**Spread by price decile (from [2604.24366]):**
- Longshot markets (price < 0.10): spreads of **1,300–1,800 basis points** — extreme adverse selection
- Central-price markets [0.40–0.60]: ~400 bps
- **Implication:** Avoid entering Kalshi positions that match to thin longshot Polymarket markets. Only trade where Polymarket price is in the 0.30–0.70 range.

**Depth decay near resolution:**
- Log depth ~ 0.305–0.818 × log(seconds_to_close)
- Depth halves roughly every time remaining halves
- **Implication:** Exit Kalshi positions at ≥72h to close, not <24h. Depth evaporates near resolution.

**Maker concentration:**
- Median Herfindahl 0.031 (~32 effective makers)
- p90 = 0.119 (~8 effective makers)
- **Implication:** Markets with few makers (HHI > 0.15) are more susceptible to informed flow moving price.

---

## 3. Harvard Law 5-Signal Informed Trader Screen

From [Harvard Law paper] — identifies 210,718 suspicious wallet-market pairs, **69.9% win rate** (>60 std devs above null), **$143M anomalous profit** Feb 2024–Feb 2026.

**Unit of analysis:** wallet-market pair (a wallet may be informed on one event but not others).

**Five signals (combine into composite score):**

| Signal | Description | Implementation |
|---|---|---|
| Cross-sectional bet size | Position size vs. other traders in same market, same window | z-score of `usd_amount` within market |
| Within-trader bet size | Consistency of sizing vs. that wallet's own history | z-score vs. wallet's trailing avg `usd_amount` |
| Profitability | Realized return on this specific wallet-market pair | `cashPnl` from positions API; or resolution × entry price |
| Pre-event timing | Trade placed immediately before public announcement | Timestamp proximity to known event times |
| Directional concentration | Bet aligned with eventual resolution direction | Compare `taker_direction` to market resolution outcome |

**Composite:** Sum z-scores, flag wallet-market pairs in top 5% as suspicious.

**Key insight:** The timing signal (pre-event placement) was the most discriminating. Informed traders don't wait — they enter within minutes of learning the information.

---

## 4. Market Maturation Signal (Kyle's λ Trajectory)

From [2603.03136] — Trump YES market:
- **Early (Jan 2024):** λ ≈ 0.53 (thin, high impact)
- **October 2024:** λ ≈ 0.01 (deep, low impact)
- **Arbitrage half-life:** hours → <1 minute

**Implication:** As Polymarket prices converge toward resolution, the Kalshi arbitrage window compresses. λ trajectory is a measure of remaining opportunity. Enter early when λ is still high (thick spread, slower convergence), exit when λ is low (rapid convergence, arb window closing).

**Disagreement signal:** Two-sided capital inflow correlation rising from 0.17→0.76 in the final weeks indicates institutional money entering both sides — sophisticated disagreement, not manipulation. When you see balanced large flows, don't assume one direction is informed.

---

## 5. Wash Trading Baseline

From [2604.24366]:
- Median wash share: **0.97%** of volume
- p90: 4.5%
- Max: 22.2%

**For our bootstrap:** Filter wallets with >10% wash-trading patterns (near-zero net position across window) before scoring them. These inflate volume stats without genuine directional conviction.

---

## 6. What the Poly_Data CSV Enables (vs. API Approach)

| Signal | API Bootstrap | CSV (poly_data) |
|---|---|---|
| Whale tier classification | Partial (positions only) | Full (all fills, all wallets) |
| VPIN | Cannot reconstruct | Yes (volume buckets from fills) |
| Kyle's λ | Cannot reconstruct | Yes (price + signed volume) |
| ILS (pre-resolution leakage) | No | Yes (resolution-anchored) |
| Timing signal | No | Yes (timestamp precision) |
| Wash trading filter | No | Yes (net position per wallet) |
| Directional ratio | Approximate | Exact (`maker_direction`) |

**CSV is significantly superior for all signals except real-time copy-trading** (where the live API is necessary for latency).

---

## 7. Implementation Priority

Based on research evidence, build in this order:

1. **Taker OFI rolling window** — highest evidence, direct from `taker_direction` + `usd_amount`
2. **Harvard Law 5-signal wallet screen** — validated at $143M scale, replace our current win-rate-only scoring
3. **Kyle's λ spike detection** — relative to 7-day median; signals thin liquidity → fast convergence
4. **VPIN** — more robust than OFI when volume is lumpy
5. **ILS (pre-resolution leakage)** — identifies markets with genuine insider activity
6. **Wash trading filter** — clean wallet list before any scoring

---

## 8. Critical Cautions

- **Do NOT use feed-inferred trade direction.** Only use on-chain `maker_direction`/`taker_direction`. Feed inference is ~59% accurate — barely better than random.
- **Do NOT apply textbook Kyle's λ.** On-chain noise perturbs the equilibrium. Use λ as a relative measure only.
- **Avoid longshot markets** (Polymarket price < 0.15 or > 0.85) — spreads are 3–4× wider; adverse selection is extreme.
- **Depth evaporates near resolution.** Enter at 4–168h to close (our existing filter). Exit at 72h+ if possible.
- **Sports markets dominate** (77.9% in empirical samples). Signals calibrated on sports may not generalize to crypto/politics.
