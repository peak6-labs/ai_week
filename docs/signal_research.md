# Prediction-market signal research

Evidence base for the signals in this pipeline, plus candidates evaluated and
(in some cases) rejected. Goal: only build signals with real empirical support,
not hype. Each entry notes source quality.

## Implemented — well supported

### Favorite-longshot bias (FLB) → `kalshi_bias`
Longshots are systematically overpriced and favorites underpriced. One of the
most durable anomalies in betting/prediction markets — documented for decades and
shown not to arbitrage away.
- *The Temporal Evolution of Mispricing in Prediction Markets* (Economics
  Letters, ScienceDirect) — peer-reviewed. FLB exists in prediction markets and
  **its magnitude changes in the final trading days** → supports our
  horizon-scaling of the correction.
- *Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics* (arXiv
  2602.19520) — calibration differs by domain → supports treating political vs
  non-political markets differently (we do).
- **Caveat:** FLB is an *average* tendency; on any single market it's weak. We
  size it modestly and let the paper-trade loop validate it.

### Cross-venue price discrepancy → `polymarket-price`, `sportsbook-odds`
Law-of-one-price violations across venues are real and exploitable when a
genuinely equivalent contract is priced differently.
- *Semantic Non-Fungibility and Violations of the Law of One Price in Prediction
  Markets* (arXiv 2601.01706) — **important caveat**: contracts that look
  identical often aren't (subtle resolution-criteria differences), so a raw price
  gap can be a phantom edge. Our matchers require a real title/competitor match
  and we keep a min-gap threshold; sportsbook moneylines are the cleanest case.
- *Arbitrage Analysis in Polymarket NBA Markets* (arXiv 2605.00864) — sports
  markets show recurring arbitrage vs sportsbooks → motivates `sportsbook-odds`.

### Microstructure direction → `microstructure`
Order-flow imbalance / momentum / range position. Weaker and noisier evidence
than the above; treated as a low-weight, high-uncertainty signal pending
paper-trade validation.

## Recommended next — well supported, not yet built

### Event coherence / law of one price *within* an event → `event-coherence` (recommended; preconditions below)
For a set of **mutually exclusive** outcomes in one event (e.g. "who wins the
primary"), the YES prices should sum to ~1. A sum > 1 (overround) means the set
is collectively overpriced; < 1 means underpriced. Normalizing each YES price by
the sum is a direct, model-free calibration — and it is *independent* of the
price-derived microstructure/FLB signals.
- *Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets* (arXiv
  2508.03474) and the law-of-one-price paper above support this.
- **Critical caveats (why it's not built yet):**
  1. Only valid for genuinely mutually-exclusive sets. Kalshi also has *threshold
     ladders* (temp ">80", ">85") that are nested/monotonic and must NOT sum to 1
     — the implementation must detect and exclude these.
  2. **The scout filters markets (OI/volume/time) before grouping**, so an
     event's outcome set is usually *incomplete* — the YES sum is then spuriously
     low and the normalization is biased. Doing this correctly requires fetching
     the event's *full, unfiltered* outcome set (a `get_event` call per event).
  Build only after the full-outcome-set fetch is wired; shipping it on filtered
  subsets would manufacture a false edge.

## Evaluated — treat with caution / not building yet

- **Adverse selection / informed-trading (VPIN, PIN).** *Adverse Selection in
  Prediction Markets: Evidence from Kalshi* (Stanford Law) is an **op-ed/opinion
  piece**, not peer-reviewed empirics. The order_flow VPIN signal exists but we
  do not over-weight it; needs our own validation data before trusting.
- **Momentum / "hot hand" continuation.** Mixed evidence — some studies find
  short-horizon momentum, others overreaction/reversal. We capture both
  directions in `microstructure` (drift + range mean-reversion) rather than
  betting on one; let outcomes decide the weight.
- **Social-sentiment as alpha (generic "X buzz").** Easy to overfit to noise.
  Kept low-weight and the scorer drops non-informative (uncertainty ≥ 0.99) X
  estimates.

## Method
Every signal's real value is measured by the paper-trade calibration loop
(`recommendations` → `recommendation_marks`) and the `signals` Brier scores on
resolution. Weights start from priors above and are tuned from that record — we
trust the data over the literature's averages.
