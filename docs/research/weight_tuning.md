# Signal-Weight & Threshold Tuning — Methodology and (Pre-Data) Proposal

Research date: 2026-06-03. **Status: read-only analysis. No config or code changed,
no trades executed.** This document defines the tuning framework, reports the
(tiny) preliminary data, and stages a concrete proposal to revisit once enough
paper marks have accumulated.

Inputs read: `data/paper/recommendations.jsonl`, `data/paper/marks.jsonl`,
`runtime_config.json`, `scripts/score_signals.py`, `scripts/paper_track.py`,
`kalshi_trader/paper.py`, `docs/research/kalshi_fees.md`.

> ⚠️ **The dataset is currently 13 recommendations, of which only 2 are
> resolved** (both `insufficient_edge`, both sourced from
> `microstructure`+`kalshi_bias`, both NO-side). **No `worth_trading` or
> `approved` recommendation has resolved yet.** Nothing below licenses a real
> weight change today. The deliverable is the *method* plus a *staged* proposal.

---

## 0. How the data is recorded (so the metrics are interpreted correctly)

From `scripts/paper_track.py` and `kalshi_trader/paper.py`:

- **`predicted_prob` is the side-win probability**, not the YES probability:
  `predicted_prob = combined_probability if side=="yes" else 1 - combined_probability`
  (`paper_track.py:121`). So a NO-side rec with `predicted_prob = 0.94` means "we
  think NO wins with 94% probability." Brier scoring must therefore be computed
  against the **side outcome** (1 if the chosen side won, else 0), *not* against
  the YES outcome.
- **`edge_cents` recorded is the fee-adjusted edge**, not the raw edge
  (`paper_track.py:122` reads `fee_adjusted_edge` first). This is convenient: the
  edge-bucket boundaries in `paper.py` (`0, 2.5, 5.0, 7.5, 10.0`) line up
  *directly* with the `worth_trading` bar (`fee_adjusted_edge > 5.0`). The
  `[5,7.5)` bucket and up are exactly the trades that clear the bar.
- **`disposition`** is `worth_trading` (cleared edge+source bar) or
  `insufficient_edge` (below bar). `approved` (risk-passed onto the live slate) is
  defined in `paper.py` but is not currently emitted by `paper_track.py` — see
  §4 instrumentation.
- **Entry cost is taker** on the chosen side: YES at ask, NO at `100 − yes_bid`
  (`paper.entry_price_cents`). Marks are conservative exit prices (sell into the
  bid / `100 − ask`), and settlement is the 100/0 binary payout. So recorded P&L
  already embeds the spread you would cross to exit a still-open position.

---

## 1. Tuning methodology

Three independent calibration questions, three metrics. All are already wired in
`kalshi_trader/paper.py`; the work is feeding them enough resolved marks and then
reading them with the right statistical discipline.

### 1.1 Per-source predictiveness — Brier score

For each source, gather every resolved rec whose `sources` list contains it, and
score the **side-win probability** against the **side outcome**:

```
brier_one = (predicted_prob − side_outcome)²          # side_outcome ∈ {0,1}
brier_source = mean(brier_one over resolved recs carrying that source)
```

Reference points:
- **0.25** = an uninformative "always 0.5" predictor. Any source materially above
  0.25 is *anti-predictive at the confidence it claims* (either wrong-way or
  over-confident).
- **0.0** = perfect. Good calibrated sources on binary primaries should land well
  under 0.25 (often 0.05–0.15 once volume accumulates).

Because sources co-occur (most recs carry `microstructure`+`kalshi_bias`
together), per-source Brier is **confounded** until we have recs where sources
appear in different combinations. The clean attribution path is the **Supabase
`signals` table**, which stores each source's *individual* estimate and
auto-computes a per-source Brier on resolution (per `paper.py` module docstring).
That is the source of truth for isolating one signal's contribution; the JSONL
`performance_by_source` is a coarse co-occurrence view. (Per task constraints this
analysis did not query Supabase; it is the right tool once N is sufficient.)

**Weight update rule (once trustworthy):** map relative Brier skill to relative
weight, not raw Brier to absolute weight. E.g. rank sources by
`skill = 0.25 − brier_source` (positive = beats coin flip); set the new weight
proportional to a smoothed, clipped skill, keeping the current weights as the
prior and moving fractionally toward the data:

```
weight_new = clip( weight_old × (1 + α · (skill_source / skill_mean − 1)), 0.3, 1.0)
```

with a small `α` (e.g. 0.25) so a single noisy quarter cannot swing a weight.
Calibration (not just discrimination) also matters: track a **reliability
curve** (predicted-prob bucket → realized win-rate); a source that is directionally
right but over-confident should have its *uncertainty* raised rather than its
*weight* cut.

### 1.2 Per-disposition performance — win-rate & avg P&L

`performance_by_disposition()` gives `marked, wins, win_rate, avg_pnl_cents` for
`worth_trading` / `insufficient_edge` (/ `approved` once emitted). The decisive
comparisons:

- `worth_trading` (or `approved`) should show **avg P&L > 0** and a win-rate
  consistent with its edge. If it is ≤ 0 with adequate N, the signal stack or the
  bar is broken.
- `insufficient_edge` should show **avg P&L ≤ ~0**. If the rejected bucket is
  *also* profitable at similar rates, the filters are leaving money on the table
  and the bar is too high (see §1.4).

P&L must be read as **avg P&L per contract in cents**, and dominated by
settlement outcomes (a lost binary is −entry, e.g. −93¢; a win is
+(100−entry)). With small N, a single confident loss craters the average — so
pair avg P&L with **median** and with **win-rate vs. break-even win-rate**
(`break_even_winrate = entry / 100` for the chosen side).

### 1.3 Per-edge-bucket calibration — is the 5¢ bar right?

`performance_by_edge_bucket()` buckets by recorded (fee-adjusted) `edge_cents`:
`(-inf,0) [0,2.5) [2.5,5) [5,7.5) [7.5,10) [10,inf)`.

**What "the 5¢ bar is right" looks like in the data** (with N per bucket ≥ ~30):

1. **Monotonic P&L:** avg P&L per contract rises with the edge bucket. If
   `[2.5,5)` is already solidly profitable while `[5,7.5)` is not, the bar is too
   *high*. If everything below `[7.5,10)` is a coin flip, the bar is too *low*.
2. **Break-even crossing near 5¢:** the bucket whose realized avg P&L crosses
   from ≤0 to >0 should be the one containing the bar. If the crossing is at, say,
   `[2.5,5)`, that argues for lowering the bar; if at `[7.5,10)`, for raising it.
3. **Realized vs. predicted edge:** within each bucket compare mean recorded
   `edge_cents` to realized avg P&L. They should track. A persistent gap (realized
   ≪ predicted) means the edge is systematically **over-estimated** — the fix is
   recalibrating signals/uncertainty, not moving the bar.

### 1.4 Does maker entry justify lowering the 5¢ bar?

From `docs/research/kalshi_fees.md`: taker entry fee is `0.07·P·(1−P)·100`
(≤1.75¢/contract, peaking at 50¢). A **maker/limit** entry pays **0** on
non-maker-fee markets and **25% (0.0175 coeff)** on maker-fee markets. The current
`worth_trading` bar is `fee_adjusted_edge > 5.0`, where `fee_adjusted_edge =
raw_edge − taker_fee`.

The *fee* component the bar is currently absorbing is at most ~1.75¢. So **maker
entry can justify lowering the bar by the fee delta only — roughly 0.6–1.75¢,
i.e. toward ~4.0¢ on non-maker-fee markets** — *and only if fill is modeled*. A
resting order is not guaranteed to fill, and tends to fill adversely (the market
came to you because it moved against you). The honest formulation, per the fees
doc §5(b):

```
effective_edge = raw_edge − maker_fee(market) ;   maker_fee = 0 or 0.0175·P·(1−P)·100
expected_edge  = fill_probability × effective_edge        # fill_probability < 1
worth_trading  = expected_edge > BAR
```

So the bar itself need not move; instead **swap the taker-fee term for a
maker-fee term and multiply by an explicit fill probability**. Lowering the
literal `5.0` constant is only safe when `fill_probability` is high
(deep/liquid markets) — otherwise the fee savings are an illusion. Do **not**
lower the bar on fee grounds alone until the pipeline actually posts maker orders
and we have fill-rate data.

### 1.5 Statistical discipline — minimum N before trusting a move

Binary outcomes are high-variance; one settlement is ±~90¢. Guardrails:

- **Per edge bucket:** ≥ **30** resolved marks before reading its win-rate; ≥
  **50** before moving the bar. Report a Wilson 95% CI on win-rate, not a point
  estimate.
- **Per source (via Supabase per-source Brier):** ≥ **30** resolved marks where
  that source contributed, and ideally appearing in ≥2 distinct source
  combinations, before adjusting its weight. Move weights fractionally (`α ≤
  0.25`) and never to the extreme of the range.
- **Disposition comparison:** need both `worth_trading` and `insufficient_edge`
  buckets at N ≥ 30 each before concluding the filter is mis-set.
- **Diversify the recorded universe:** today every resolved rec is the same source
  pair on NO-side election primaries. Until recs span categories, sides, and
  source mixes, *any* aggregate is a statement about one narrow regime.

---

## 2. Preliminary findings (current data — N=2 resolved, treat as noise)

Computed by running `paper.performance_*` against the local JSONL and a manual
Brier pass. **These are reported for completeness only; none meet the N bars in
§1.5.**

**Overall:** `marked=2, wins=1, win_rate=0.5, avg_pnl_cents=−44.5`.

**By disposition:**

| disposition        | marked | wins | win_rate | avg_pnl_cents |
|--------------------|-------:|-----:|---------:|--------------:|
| worth_trading      | 0      | 0    | —        | —             |
| insufficient_edge  | 2      | 1    | 0.50     | −44.5         |

The two resolved recs are *both* `insufficient_edge`. **We have zero resolved
evidence on the trades the system would actually take.** The −44.5¢ average is one
correct call (`KXMTPRIMARY-01R26-CJAC`, +4¢) and one confident miss
(`KXNJPRIMARY-02D26-ZMUL`, −93¢).

**By source** (fully confounded — both sources co-occur on both recs):

| source         | marked | win_rate | avg_pnl_cents |
|----------------|-------:|---------:|--------------:|
| microstructure | 2      | 0.50     | −44.5         |
| kalshi_bias    | 2      | 0.50     | −44.5         |
| x_grok_*       | 0      | —        | —             |

`x_grok` carried no resolved recs (and `agent_x_enabled=false` in config, so the
one rec that lists `x_grok_*` slices, `MAYORLA26SPRA-65`, is still open).

**By edge bucket** (recorded edge is fee-adjusted):

| bucket     | marked | win_rate | avg_pnl_cents |
|------------|-------:|---------:|--------------:|
| [2.5,5)    | 2      | 0.50     | −44.5         |
| all others | 0      | —        | —             |

**Per-rec Brier (side-win prob vs. side outcome):**

| ticker                  | pred_side | outcome | brier |
|-------------------------|----------:|--------:|------:|
| KXMTPRIMARY-01R26-CJAC  | 0.9386    | 1       | 0.0038 |
| KXNJPRIMARY-02D26-ZMUL  | 0.9056    | 0       | 0.8201 |
| **mean**                |           |         | **0.412** |

Mean Brier 0.41 is *worse than a 0.5 coin flip* (0.25) — but that is one
over-confident miss on a single primary. The CJAC call was excellent (0.004). At
N=2 the mean is uninformative; the only honest read is "one confidently-wrong NO
on a low-edge election primary," which is exactly the kind of trade the
`insufficient_edge` filter correctly declined to act on.

**Tentative (non-actionable) observations, all pending N:**
- The filter did its job: it *rejected* both resolved recs, and one would have
  been a −93¢ disaster. Early sign the edge bar is doing useful work — but with no
  resolved `worth_trading` rec we cannot yet confirm it isn't *also* rejecting
  winners.
- Confident NO-side calls on election primaries (entry 90–96¢) carry brutal
  downside (−90¢+ when wrong) for ≤4¢ upside. Even a 94%-correct edge needs
  ~96% calibration to break even at 96¢ entry (`break_even_winrate = entry/100`).
  This high-entry-price regime deserves its own bucket (see §4).

---

## 3. PROPOSED changes — staged, NOT applied

Each proposal lists its **trigger N** (data required before it may be applied) and
is **off** until then. None are applied in this PR.

### 3.1 Threshold proposals

| # | Change | Trigger before applying | Rationale |
|---|--------|------------------------|-----------|
| T1 | **Keep `worth_trading` bar at `fee_adjusted_edge > 5.0` for now.** | N/A (status quo) | No resolved `worth_trading` data; do not move blind. |
| T2 | **Add a maker-aware edge: replace taker fee with `maker_fee × fill_probability` term when entering passively**, rather than lowering the literal 5.0. | Pipeline posts maker orders + ≥30 fills with measured fill rate. | Fees doc §5(b): savings are real (0.6–1.75¢) but illusory without fill modeling. |
| T3 | **Only if** edge-bucket data shows `[2.5,5)` solidly profitable AND `[5,7.5)` not better: lower bar toward ~4.0¢ (taker) / ~3.5¢ (maker). | ≥50 resolved per bucket; monotonicity + break-even crossing per §1.3. | Bar should sit at the realized break-even, wherever the data puts it. |
| T4 | **Add an entry-price guardrail for high-priced NO/YES legs** (e.g. require larger edge or higher source agreement when entry ≥ 90¢). | ≥30 resolved recs with entry ≥ 90¢. | Asymmetric payoff: ≤4¢ up vs. ~90¢+ down; one miss (NJ) dominated all P&L. |

### 3.2 Weight proposals (all gated on Supabase per-source Brier at N ≥ 30/source)

Current weights: `noaa 0.85, poly_price 0.75, poly_whale 0.6, x_grok 0.6,
x_claude 0.75, market_maker 0.65, kalshi_bias 0.70` (note: `kalshi_bias` weight is
**hard-coded to 0.70 in `score_signals.score_kalshi_bias`**, line ~214, and does
*not* read `runtime_config`; `runtime_config` has no `weight_kalshi_bias` key —
flag this inconsistency, see §4).

| # | Change | Trigger | Rationale |
|---|--------|---------|-----------|
| W1 | Re-weight by relative Brier skill using the smoothed/clipped rule in §1.1 (`α=0.25`, clip [0.3,1.0]). | ≥30 resolved/source, ≥2 source combinations. | Move weight toward demonstrated skill, fractionally. |
| W2 | If a source is directionally right but over-confident (good win-rate, high Brier from over-confidence), **raise its `uncertainty_*` instead of cutting weight.** | reliability curve at N ≥ 30/source. | Fixes calibration without discarding a useful signal. |
| W3 | Make `kalshi_bias` weight config-driven (`weight_kalshi_bias`) so it can be tuned at all. | code change (separate PR; not this read-only task). | Currently untunable; blocks W1 for this source. |
| W4 | Leave `min_agents=2` as-is; revisit only if single-source recs (e.g. whale-only) show distinct, trustworthy Brier. | per-source Brier by `n_sources`. | Source-count gate is cheap insurance against thin evidence. |

**Net stance:** the only change that is *defensible to even prepare* now is the
maker-fee modeling (T2) and the high-entry-price guardrail concept (T4), because
both are grounded in the fee research and the payoff asymmetry rather than in 2
P&L points. Every weight move (W1–W3) is parked until the per-source Brier has
N ≥ 30.

---

## 4. Instrumentation / metrics to add

To make the above computable and trustworthy:

1. **Emit `approved` disposition.** `paper_track.py` only records
   `worth_trading` / `insufficient_edge`; `performance_by_disposition` already
   supports `approved`. Record risk-passed slate trades as `approved` so we can
   compare the *actually-traded* bucket, not just "cleared the edge bar."
2. **Per-source Brier from Supabase, surfaced locally.** The `signals` table
   auto-computes per-source Brier on resolution; add a read-only report
   (`scripts/`) that pulls it and prints Brier + reliability curve per source.
   This is the only way to *de-confound* co-occurring sources.
3. **Reliability/calibration curve** (predicted-prob bucket → realized win-rate)
   per source and overall — distinguishes "wrong direction" (cut weight) from
   "over-confident" (raise uncertainty).
4. **Wilson confidence intervals** on every win-rate in the scorecards, and a
   `marked`-count gate that suppresses any bucket/source below the §1.5 N before
   it is allowed to influence a tuning decision.
5. **Entry-price bucket** alongside the edge bucket (e.g. `<50, 50–80, 80–90,
   ≥90`) to expose the asymmetric-payoff regime that dominated current P&L.
6. **Realized-vs-predicted edge** per bucket (mean recorded `edge_cents` vs. mean
   realized P&L) to detect systematic edge over-estimation independent of the bar.
7. **Round-trip / exit-fee flag** on recs that exit before settlement, so the fee
   model (entry-only today) can be corrected to entry+exit for those — per fees
   doc §5(a.2). Holding to settlement remains entry-fee-only.
8. **Fill-rate logging** once maker entry is introduced — prerequisite for T2/T3.

---

## 5. Summary

- Framework and metrics are in place (`performance_by_source/disposition/edge_bucket`
  + Supabase per-source Brier). The missing ingredient is **resolved volume**.
- Current data (N=2 resolved, both `insufficient_edge`, both same source pair, both
  NO-side primaries) supports **no** weight or threshold change. The filter
  correctly declined both, one of which would have lost 93¢.
- **Do not move the 5¢ bar on fee grounds.** Maker entry saves ≤1.75¢ and only if
  it fills; the right change is a maker-fee + fill-probability term (T2), not a
  lower constant.
- Gate every future move on the N thresholds in §1.5 (≥30 per source/bucket,
  fractional weight moves, Wilson CIs), and add the instrumentation in §4 —
  especially emitting `approved` recs and pulling de-confounded per-source Brier
  from Supabase.
