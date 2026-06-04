---
date: 2026-06-04
author: Claude (Opus 4.8)
status: deliverable
purpose: Self-contained briefing + deep-research prompt to send to another LLM
topic: Why the Kalshi "mentions" trading signal has negative predictive skill, and how to make it tradeable
companion: thoughts/shared/research/2026-06-04-mentions-signal-effectiveness.md (the raw backtest findings)
---

# Kalshi "Mentions" Signal — Discovery Brief & Deep-Research Prompt

> **For the receiving model:** This document is fully self-contained. You do not
> need access to any codebase. Read the whole thing, then answer the **Research
> Questions** at the end. The goal is concrete, testable methods to turn a signal
> that currently has *negative* predictive skill into one that is *tradeable*.

---

## 1. TL;DR

We trade on **Kalshi**, a regulated US prediction-market exchange where each
contract is a yes/no question that settles to $1 (yes) or $0 (no). One contract
family is **"mentions" markets**: *"Will `<person>` say `<word/phrase>` in
`<venue>`?"* — e.g. *"Will Jerome Powell say 'recession' in the next FOMC press
conference?"* or *"Will an announcer say 'alley-oop' during tonight's NBA game?"*

Our signal estimates **P(the phrase gets said)** for each such market. We
backtested it on **455 settled markets** and found it has **negative skill** — it
is *worse than guessing the base rate* and *worse than always betting "yes."* Its
calibration is **inverted**: when the model says a phrase is very unlikely it
tends to happen, and vice-versa.

We have identified *why* (the model measures the wrong quantity), and we have a
stop-gap (suppress the bad reads, trade nothing). **We now need research into what
would actually work.** This brief gives you the full problem, the exact method
that failed, the failure evidence, and the data sources we can access.

---

## 2. What a "mentions" market is, concretely

A Kalshi mentions market resolves YES if a specific phrase is spoken in a specific
bounded real-world event, NO otherwise. There are two broad subtypes, and **both
are in scope for this research**:

**Subtype A — Politics / officials / hearings.**
*"Will `<official>` say `<phrase>` in `<hearing / press briefing / floor speech /
press conference>`?"* Examples: Powell saying "recession" at an FOMC presser; a
senator saying "shutdown" in a committee hearing; the President saying a given
word at a briefing. The event is a single, scheduled, finite-duration occasion.
These are lower-frequency but strategically interesting (macro-adjacent, and the
speaker is identifiable).

**Subtype B — Sports / entertainment broadcasts.**
*"Will `<a broadcaster / the broadcast>` say `<player name / play-type term /
celebrity name>` during `<tonight's game / this episode>`?"* Examples: a courtside
celebrity's name during an NBA game; "airball" or "alley-oop" during a broadcast;
a contestant's name on *Love Island*. These recur per-game/per-episode, so they
are **high-frequency and high-volume** — they dominated our backtest sample
(406 of 455). The "speaker" is effectively the broadcast itself, not one person.

Key structural facts that matter for modeling:
- The event is **bounded in time** (a ~45-minute presser, a ~2.5-hour game, a
  ~1-hour episode). The question is "≥1 occurrence within this window," not "is
  this word generally common."
- The phrase is matched as an **exact/substring string** at settlement (Kalshi
  reads a transcript or official source).
- Markets settle quickly (same day to a few weeks), so a usable signal must be
  produced **before close** and there is no slow accumulation of evidence.

---

## 3. The signal pipeline as built (what already exists)

The production signal is multi-source and "speaker-routed." A registry maps each
speaker to the data sources that actually cover them. The components:

1. **`mentions_base` (the core probability estimate)** — *intended* to fuse two
   inputs by evidence weight:
   - a **speaker-attributed transcript count**: of the documents we have where
     *this specific person* spoke in the relevant venue, what fraction contained
     the phrase (`match_count / document_count`); and
   - the **GDELT TV base rate** (defined below) as a fallback/corroborator.
   The fusion is `weight_corpus = docs / (docs + 5)`, blending the two
   probabilities, with the corpus-attributed term dominating once we have enough
   documents (≥12 → "corpus-backed," treated as real evidence).

2. **`mentions_live`** — a near-real-time detector: if the phrase shows up in the
   last ~day of closed-captions on the speaker's stations while the market is
   open, emit a high-probability "it's already happening" signal.

3. **`x_grok_profile`** — a leading indicator: did the speaker's own X (Twitter)
   accounts post about the topic recently? (Modest weight; a predictor, not a
   measurement.)

4. **`hearing_schedule`** — a near-deterministic **veto**: if the relevant
   congressional hearing is canceled/postponed or not on the calendar before the
   market closes, the speaker *cannot* say the phrase there → force probability
   near 0. (Genuinely independent of the language model above.)

**The critical production fact:** the **speaker-attributed transcript corpus is
empty** (the archive has never been populated at scale). So in practice the
tradeable component collapses to **GDELT-only** — input #1 degrades to just the
GDELT TV base rate. That is the regime we backtested, and it is the regime that
fails.

### What the "GDELT TV base rate" is

**GDELT 2.0 Television API** indexes the Internet Archive's TV News Archive:
closed-caption text from 150+ US stations since 2009 (including C-SPAN/C-SPAN2/
C-SPAN3, which carry hearings and floor proceedings, and CNN/Fox News/MSNBC). For
a phrase query it returns, per time period, the **percent of 15-second caption
clips that matched the phrase**. We reduce this to: *the fraction of monthly
periods in which the phrase appeared at all (value > 0)* and call that the
unconditional probability the phrase gets said in a comparable broadcast period.
For sports/entertainment we proxy the broadcast with national news (CNN).

**That reduction is the suspected core error** — see §5.

---

## 4. The discovery: the backtest

**Method.** For every *settled* mentions market in five recurring series, we read
the realized outcome (yes/no) and the tracked phrase, reconstructed the GDELT base
rate **as of the market's close** (dropping later data — a look-ahead guard),
produced the model probability, and scored predicted-vs-realized.

- **Sample: 455 settled markets** — NBA 132, NHL 120, MLB 124, FOMC/Fed 48,
  *Love Island* 31. (53 more had no GDELT coverage; 24 had no parseable phrase.)
- **All 455 ran in the GDELT-only regime** (corpus empty), so this measures the
  fallback, *not* the speaker-attributed tier.

**Scoring definitions** (so the numbers are interpretable):
- **Brier score** = mean squared error between predicted probability and the 0/1
  outcome. Lower is better. The benchmark is the *naive* Brier from always
  predicting the overall base "yes" rate.
- **Hit-rate** = fraction of decisive (≠0.5) predictions whose >0.5 / <0.5 side
  matched the outcome. Benchmark = always predict the majority class.

### Result — negative skill

| Metric | Model | Benchmark |
|---|---:|---:|
| Brier score | **0.395** | 0.247 (always predict base rate) |
| Hit-rate | **0.514** | 0.554 (always predict "yes") |
| Base "yes" rate | 0.554 | — |

The model is **worse than a constant** on both metrics. It actively destroys
information.

### Calibration is inverted, in every band

| Predicted band | Count | Mean predicted | **Actual yes-rate** |
|---|---:|---:|---:|
| [0.00, 0.15) | 54 | 0.083 | **0.648** |
| [0.15, 0.50) | 62 | 0.278 | 0.516 |
| [0.50, 0.85) | 40 | 0.706 | **0.325** |
| [0.85, 1.01) | 299 | 0.971 | **0.575** |

When the model says ~8% the phrase actually appeared ~65% of the time; when it
says ~97% it appeared only ~58%. There is **no band where the read is
trustworthy** — the relationship is inverted/flat.

### Per-series — uniformly bad (every Brier exceeds the ~0.25 naive line)

| Series | n | yes-rate | Brier |
|---|---:|---:|---:|
| NBA player-mentions | 132 | 0.55 | 0.420 |
| NHL player-mentions | 120 | 0.59 | 0.387 |
| MLB player-mentions | 124 | 0.66 | 0.317 |
| FOMC/Fed mentions | 48 | 0.33 | 0.507 |
| *Love Island* mentions | 31 | 0.32 | 0.459 |

### The telling examples

- **Over-predicted (model ~high, didn't happen):** `trump` 0.99, `lebron` 0.97,
  `alien` 0.97 — words that are *ubiquitous on national TV* regardless of whether
  they were said in the specific broadcast/hearing in question.
- **Under-predicted (model ~0, did happen):** `chalamet` 0.03 (a courtside
  celebrity, named by the game announcers), `airball` 0.01 (a basketball term,
  said in the game) — *context-specific* words nearly invisible to national news
  yet very likely in the relevant event.

---

## 5. Root-cause hypothesis: the model estimates the wrong quantity

The market asks **P(phrase is said by a specific source in one specific bounded
event)**. The GDELT-only model instead measures **how generally common the phrase
is on national television over years.** These are different quantities, and the
mismatch produces the inversion through (at least) five compounding flaws:

1. **Wrong estimand.** "Fraction of monthly periods with *any* mention across a
   station" ≈ "is this word generally in the news," not "will *this* event
   contain it." Ubiquitous words saturate near 100%; rare-but-on-topic words
   read ~0%. That is exactly the inversion observed.

2. **No speaker attribution.** GDELT counts the word *anywhere on the station*,
   not utterances by the target person. "trump" saturates CNN whether or not
   Trump himself speaks; "powell" can appear because a journalist said it. The
   whole point of a mentions market is *who* says it — the proxy ignores the who.

3. **No exposure / event-length modeling.** P(≥1 occurrence) in a bounded event
   scales with the event's duration and the per-unit-time rate (think a Poisson
   exposure model). A months-long presence fraction has no notion of "this is a
   45-minute presser vs a 3-hour game," so it cannot produce an event-conditional
   probability.

4. **No conditioning on the specific occasion.** It ignores everything that makes
   *this* event distinctive: the hearing's announced topic, the current news
   cycle, the game's matchup and who is actually on the roster/active tonight,
   the episode's storyline. For sports especially, "is this player even playing?"
   is nearly dispositive and entirely absent.

5. **Coverage/measurement bias.** National-news proxying for a sports broadcast
   (using CNN as a stand-in for the game feed) measures a completely different
   linguistic distribution than the actual broadcast booth.

---

## 6. What we currently do about it (the stop-gap), and what's unvalidated

- **Suppression gate:** any GDELT-only read is now flagged non-informative so the
  trade scorer drops it (we require "corpus-backed" evidence before trading).
  Practical effect: with the corpus empty, the scan currently marks **every**
  mentions market as non-tradeable. Honest, but it means we trade *nothing* in
  this category.
- **The corpus-backed tier is UNVALIDATED (n = 0 in the backtest).** The premise
  — that speaker-attributed, venue-specific transcript counts *would* predict —
  is intuitive but has **never been measured**, because we have not populated the
  archive. It might also fail (e.g. a speaker's long-run rate of saying a word may
  still not predict a single occasion well). **Do not assume the corpus tier is
  the answer; treat validating or refuting it as a first-class research question.**
- **Sample caveat:** the backtest is sports-dominated (406/455). The
  politics/hearing markets (Fed 48, *Love Island* 31) are a thin slice — their
  Brier is even worse, but with low n.

---

## 7. Data & tooling we can realistically access

Assume we can build pipelines against any free or low-cost, programmatically
accessible source. What we already have wired (or can wire):

| Source | What it gives | Limits |
|---|---|---|
| **GDELT 2.0 TV API** | % of 15-sec caption clips matching a phrase, per period, per station, since 2009; C-SPAN family (hearings/floor) + CNN/Fox/MSNBC | Free, no key. **No speaker attribution.** Phrase ≤5 words, exact-string. Captions lag ~hours; monthly granularity over long windows. |
| **GovInfo CREC API** (Congressional Record) | Daily floor statements **attributed to the speaking member** (the `members` array) → real speaker-attributed text for `congress_floor` | Free w/ api.data.gov key. Floor speeches only (not committee hearings, not pressers). |
| **congress.gov API** | Committee-meeting **schedule**: committee, chamber, date, status (scheduled/canceled/postponed/rescheduled) | Free w/ same key. Powers the hearing-cancellation veto. |
| **X / Grok** | The speaker's own X accounts; whether they posted about a topic recently (leading indicator) | API/agent access; noisy; written ≠ spoken. |
| **Speaker registry (internal)** | Maps speaker → GDELT stations, transcript venues, X handles | Currently seeded only for Trump/Biden/Vance/Powell/Yellen. |
| **Not yet wired but plausible** | Fed speech/presser transcripts (federalreserve.gov), White House briefing transcripts, sports lineup/roster/box-score & broadcast-assignment data, closed-caption archives of specific broadcasts | Mix of free + scraped. |

**Operating constraints to respect in any proposal:**
- Prefer free / free-tier / cheap sources; this is a research bot, not a funded data desk.
- Backtests must avoid look-ahead (reconstruct inputs *as of* market close).
- The signal must be producible **before the market closes** (latency matters).
- The output is a **calibrated probability** feeding a deterministic edge calc +
  Kelly sizing — so **calibration (Brier), not just direction, is what pays.**

---

## 8. Research Questions (please answer these)

The objective: **a method (or set of methods) that produces a calibrated
P(phrase said in this event) which beats the naive baselines out-of-sample**
(target: Brier well under 0.247 and hit-rate above 0.554, ideally with monotone
calibration), for **both** the politics/hearings subtype and the
sports/entertainment subtype. Where the right answer differs by subtype, say so.

1. **Right estimand & model form.** What is the correct probabilistic model for
   "≥1 occurrence of a phrase in a single bounded event"? Make the case for an
   exposure/intensity model (e.g. estimate a per-event or per-minute rate λ, then
   P(≥1) = 1 − e^(−λ·duration)) vs. a direct conditional-frequency estimate vs.
   a learned classifier. What features feed each, and how do we calibrate it?

2. **Does speaker-attributed history actually predict?** Our untested premise is
   that "how often *this speaker* said the phrase in this venue type" predicts a
   single future occasion. Is there evidence (from forecasting / NLP / political
   science literature) that individual-speaker word-use base rates are
   predictive of a single event, and how much history is needed? How would you
   design a clean backtest to validate or refute it *before* we invest in scraping
   transcripts at scale?

3. **Replacing GDELT for the right denominator.** GDELT's "% of clips on a station
   matching a phrase" conflates ubiquity with per-event probability and has no
   speaker attribution. What better *free/cheap* sources or transformations exist
   for (a) speaker-attributed spoken-word frequency and (b) event-conditional
   language? (Consider C-SPAN's own transcript/clip search, GovInfo CREC, Fed
   transcripts, sports play-by-play/transcript corpora, etc.) For sports, what
   replaces "national news as a proxy for the broadcast booth"?

4. **Sports subtype specifics.** For player-name / play-type / celebrity-name
   markets, what features dominate P(said)? (e.g. is the player active tonight;
   matchup; team; star vs role player; the announcer crew's documented verbal
   tendencies; pace/scoring environment for play-type terms like "airball"/
   "alley-oop".) Sketch a concrete, mostly-deterministic model and the data it
   needs.

5. **Politics/hearings subtype specifics.** For official-says-word markets, how
   should we weight (a) the announced topic/agenda of the specific hearing or
   briefing, (b) the current news cycle, (c) the speaker's very recent statements/
   posts, vs. (d) long-run base rate? Is the dominant edge actually the
   *event-occurrence* question (will the hearing even happen, will the speaker
   attend) rather than the language question?

6. **Is the edge structural, or in the price?** Independent of a great point
   estimate: are these markets systematically *mispriced* by other participants
   in a way we can exploit — e.g. favorite-longshot bias on near-certain phrases,
   recency/availability bias driven by the news cycle, or thin-liquidity
   anchoring? What would a microstructure/mispricing strategy look like, and how
   would we test for it?

7. **Calibration & evaluation.** Given short, sparse, heterogeneous samples across
   many distinct phrases, what is the most honest way to evaluate and calibrate
   such a signal (reliability diagrams, Platt/isotonic recalibration, per-subtype
   pooling, walk-forward validation)? How do we avoid overfitting to a few
   recurring series?

8. **Build-vs-skip recommendation.** Synthesize: which of these markets are
   plausibly tradeable with available data, which are structurally un-edgeable,
   and what is the highest-ROI first build? Be willing to conclude "GDELT-only is
   dead; here is the minimum viable replacement" — or "this subtype isn't worth
   it."

---

## Appendix — reproduce the backtest

Read-only harness over settled Kalshi markets; reconstructs the GDELT base rate
as-of each market's close, scores predicted-vs-realized, prints a calibration
table. 455-sample run summarized above. (Internal command:
`scripts/mentions_backtest.py --max-per-series 150`.)
