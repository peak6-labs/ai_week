# Example Scoring Run — Top 20 Actionable Markets

A captured example of the scoring pipeline's output, for reference. See
[README.md](README.md) for how the pipeline and signals work.

## Run metadata

| | |
|---|---|
| **Date** | 2026-06-02 |
| **Environment** | `prod` (read-only — fetches market/candle/trade/orderbook data; **places no orders**) |
| **Command** | `KALSHI_ENV=prod PYTHONPATH=. python scripts/score_markets.py --markets-file live_markets.json --top 20 --debug` |
| **Universe** | 482,115 open markets in snapshot → 570 active after filters → 205 events |
| **Ranking** | Top 20 events by average actionability score (best market per event shown) |

## Signal legend

Composite score is a weight-renormalised blend of nine signals (full detail in
[README.md](README.md#signals)). The heaviest:

- **Relative historical volume** (0.25) — today's volume vs 30-day daily average
- **Short-term volume spike** (0.20) — last hour vs prior hours
- **Price momentum** (0.15) — absolute price move over last 4h
- **Volume/OI turnover** (0.10), **OI change / new money** (0.10)
- **Intraday & weekly high-low position** (0.08 / 0.04), **order-flow imbalance** (0.07), **orderbook skew** (0.01)

**Coverage (`Cov`)** = fraction of total signal weight actually present. A high
score at low coverage rests on only a few signals — trust it less.

## Top 20 events

| # | Market / Event | What it asks | Score | Cov | Why it ranks high |
|---|---|---|---:|---:|---|
| 1 | [`KXMIDTERMMOV-MISEND`](https://kalshi.com/markets/kxmidtermmov) | Dem margin of victory, U.S. Senate | 0.967 | 33% | Maxed turnover, OI surging (new money), price pinned at top of range — **only 3 signals present; low confidence** |
| 2 | [`KXMARTINDNCOUT-26MAY`](https://kalshi.com/markets/kxmartindncout) | Ken Martin out as DNC chair? | 0.852 | 100% | **Cleanest idea** — best market 0.921 with nearly every signal firing: high volume vs history, hourly spike, new money, one-sided YES flow |
| 3 | [`KXARTISTVS-DRAKEVSBUNNY26JUN04`](https://kalshi.com/markets/kxartistvs) | Drake > Bad Bunny streams? | 0.766 | 64% | Strong hourly spike, fully one-sided YES flow, price at range high; no price move yet |
| 4 | [`KXRUBIOMENTION-26JUN03`](https://kalshi.com/markets/kxrubiomention) | What Rubio says at House hearing | 0.705 | 61% | Event today: volume spike + full momentum + one-sided flow |
| 5 | [`KXHIGHTATL-26JUN02`](https://kalshi.com/markets/kxhightatl) | Atlanta max temp >82° today | 0.693 | 75% | Same-day weather settle — spike, momentum, new money maxed across 6 strikes |
| 6 | [`KXNJPRIMARY-02D26`](https://kalshi.com/markets/kxnjprimary) | Zack Mullock = NJ-02 Dem nominee? | 0.675 | 92% | Heavy volume vs history + new money + upward momentum |
| 7 | [`KXWHPRESSBRIEFING-RFK`](https://kalshi.com/markets/kxwhpressbriefing) | RFK Jr. at a WH press briefing? | 0.660 | 99% | High historical volume, one-sided YES flow, strong momentum |
| 8 | [`KXGOVCAPRIMARYPARTY-26`](https://kalshi.com/markets/kxgovcaprimaryparty) | Who advances, CA governor top-two | 0.645 | 99% | Volume surging on both daily *and* hourly timescales + new money |
| 9 | [`KXVOTEPRIMARY-MAYORLA26SPRA`](https://kalshi.com/markets/kxvoteprimary) | Spencer Pratt ≥50% LA mayor | 0.643 | 92% | 9 markets; volume on both timescales, new money, near-total YES flow |
| 10 | [`KXLOWTLV-26JUN02`](https://kalshi.com/markets/kxlowtlv) | Las Vegas min temp 73–74° today | 0.638 | 65% | **Highest single market (0.992)** — same-day weather: spike, momentum, range-high, one-sided flow all maxed (no daily history) |
| 11 | [`KXTONYAWARDS-26BPBAAC`](https://kalshi.com/markets/kxtonyawards) | Kelli O'Hara win Best Actress (Tony) | 0.625 | 92% | Sharp price move + hourly spike + new money despite low volume vs history |
| 12 | [`KXSPOTIFYD-26JUN02`](https://kalshi.com/markets/kxspotifyd) | Top USA Spotify song today | 0.608 | 57% | Daily settle; price moved hard, full turnover |
| 13 | [`KXPERSONMENTION-26JUN02C`](https://kalshi.com/markets/kxpersonmention) | Quote during PlayStation State of Play | 0.593 | 65% | 11 markets; full momentum + range-high + decent YES flow, event today |
| 14 | [`KXDNIANNOUNCE-26MAY`](https://kalshi.com/markets/kxdniannounce) | Trump announcement re: DNI | 0.591 | 100% | **Fully corroborated** — best market 0.965, all nine signals firing; avg dragged down by quieter strikes |
| 15 | [`KXVOTEPRIMARY-LAMAYOR1R26KBASKBAS`](https://kalshi.com/markets/kxvoteprimary) | Karen Bass 25–30% LA mayor | 0.590 | 100% | Fully covered: volume both timescales, new money, one-sided flow |
| 16 | [`KXLAMAYORMATCHUP-26JUN`](https://kalshi.com/markets/kxlamayormatchup) | Bass & Pratt the nominees? | 0.588 | 99% | High historical volume, hourly spike, new money, range-high |
| 17 | [`KXCOPRIMARY-05D26`](https://kalshi.com/markets/kxcoprimary) | Jessica Killin = CO-05 Dem nominee? | 0.580 | 92% | Full turnover + strong volume vs history + new money; price flat |
| 18 | [`KXGABBARDOUT-26`](https://kalshi.com/markets/kxgabbardout) | Tulsi Gabbard leaves DNI? | 0.574 | 80% | Elevated volume vs history + new money; little price action |
| 19 | [`KXCA11PRIMARY-26`](https://kalshi.com/markets/kxca11primary) | CA-11 primary winner | 0.565 | 99% | High historical volume + new money + momentum |
| 20 | [`KXACAEXT-26JAN`](https://kalshi.com/markets/kxacaext) | ACA subsidy extension legislation | 0.561 | 45% | **Rests almost entirely on one signal** (high volume vs history); turnover and OI growth near zero — weakest-corroborated |

## How to read this

- **Highest-conviction (full coverage + signals firing):** #2 `KXMARTINDNCOUT-26MAY`, #14 `KXDNIANNOUNCE-26MAY` (best market 0.965), #15 `KXVOTEPRIMARY-LAMAYOR1R26KBASKBAS`. These score high *and* every signal corroborates.
- **High score, thin evidence — treat with caution:** #1 `KXMIDTERMMOV-MISEND` (33% coverage), #20 `KXACAEXT-26JAN` (45%), and same-day weather markets (#5, #10), whose top scores come largely from intraday spike/momentum with no daily-volume history yet.
- **Theme:** the board is dominated by near-term political/primary contracts and same-day weather/entertainment settles — where unusual flow concentrates intraday.

> **Link note:** links resolve to the **series** landing page only (the deep-link
> slug isn't derivable from the ticker — see [web_links.py](../web_links.py)).
> #9 and #15 are different events that share one series page
> (`kalshi.com/markets/kxvoteprimary`); the link lands you on the series and you
> pick the event there.
