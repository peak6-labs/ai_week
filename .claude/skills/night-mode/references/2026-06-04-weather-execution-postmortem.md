# June 4, 2026 Weather Execution Post-Mortem

Use this note as a guardrail for future night-mode and same-day weather execution.
This write-up is based on Kalshi `portfolio/fills` and `portfolio/positions`,
not on local paper rows alone.

## Source of truth

- Real execution history: Kalshi `portfolio/fills`
- Real per-market PnL: Kalshi `portfolio/positions -> market_positions`
- Local approval trail: `data/paper/recommendations.jsonl`

Important: local recommendation rows were incomplete for June 4.
`KXLOWTHOU-26JUN04-B72.5` traded live but had no matching local approved row.
Do not assume local paper records are a complete execution ledger.

## Actual June 4 live weather result

At the time of this analysis, the June 4 weather execution set was:

- 11 executed weather tickers
- 10 matched to a prior local approved recommendation
- 1 executed without a local approval trail
- Realized PnL: `-$24.23`
- Unrealized PnL: `+$0.25`
- Fees: `$1.28`
- Net PnL: `-$23.98`

## Per-ticker outcomes

| Ticker | Executed side | Approval edge | Net PnL | Verdict |
| --- | --- | ---: | ---: | --- |
| `KXLOWTHOU-26JUN04-B72.5` | YES | n/a | `-7.98` | Process gap; executed without approval record |
| `KXHIGHPHIL-26JUN04-B89.5` | NO | 20.2c | `-3.01` | Wrong |
| `KXHIGHTSEA-26JUN04-B67.5` | NO | 29.7c | `-5.78` | Wrong |
| `KXHIGHTATL-26JUN04-B84.5` | NO | 22.2c | `+6.60` | Right |
| `KXHIGHTBOS-26JUN04-B90.5` | NO | 24.9c | `+2.88` | Right |
| `KXHIGHCHI-26JUN04-T86` | YES | 68.3c | `-9.96` | Biggest miss |
| `KXHIGHAUS-26JUN04-B84.5` | NO | 29.3c | `-3.60` | Wrong |
| `KXHIGHLAX-26JUN04-B70.5` | NO | 17.6c | `-6.04` | Wrong after execution/exit path |
| `KXHIGHTHOU-26JUN04-B86.5` | NO | 18.7c | `-0.86` | Slightly wrong |
| `KXHIGHTATL-26JUN04-B82.5` | NO | 27.4c | `-2.80` | Wrong |
| `KXHIGHPHIL-26JUN04-B91.5` | NO | 35.6c | `+6.57` | Right |

## What actually went wrong

### 1. The live book lost money even though some paper marks looked positive

The earlier paper-style read overstated performance because it marked ideas
against later quotes instead of using the account's own fill and position data.
For execution review, `market_positions.realized_pnl_dollars` is the authority.

### 2. Late-day exact-band weather was overconfident

Most misses came from narrow same-day band contracts:

- `KXHIGHTSEA-26JUN04-B67.5`
- `KXHIGHPHIL-26JUN04-B89.5`
- `KXHIGHAUS-26JUN04-B84.5`
- `KXHIGHTATL-26JUN04-B82.5`
- `KXLOWTHOU-26JUN04-B72.5`

These contracts are very sensitive to the last 1-2°F of path dependence.
Large quoted edge on a narrow band did not mean low true uncertainty.

### 3. `KXHIGHCHI-26JUN04-T86` was a true forecast miss, not a parsing bug

Kalshi title:

- `Will the high temp in Chicago be <86° on Jun 4, 2026?`

The trade side and contract semantics matched. The model was simply wrong.
The error was confidence calibration: the system treated a strong ensemble view
plus order-flow confirmation as nearly locked when it was not.

### 4. Repricing and repeated approval made the book worse

June 4 showed repeated same-thesis approvals. That is not independent edge.
Once the market has already moved toward the thesis, fresh approvals should face
a much higher residual-edge bar.

### 5. Execution/accounting paths were fragmented

June 4 weather execution came through `auto_execute_weather.py -> place_order.py`,
not only `night_execute.py`. Any future review or skill logic must assume:

- `night-mode-*.jsonl` is not a complete execution ledger
- Kalshi fills/positions must be checked for truth

## What was actually right

The edge was not fake. Some weather theses were good:

- `KXHIGHTATL-26JUN04-B84.5` NO
- `KXHIGHTBOS-26JUN04-B90.5` NO
- `KXHIGHPHIL-26JUN04-B91.5` NO

The common pattern in the winners:

- clear overshoot/undershoot away from the band
- better alignment between forecast shape and settlement band
- less dependence on the final marginal degree

## Changes to make before future weather execution

### Hard execution rules

1. Treat narrow same-day band markets as a special risk class.
   Apply a materially higher edge threshold than for one-sided threshold markets.

2. Decay confidence sharply late in the day.
   If the remaining move needed is within roughly 1-2°F of the band, do not
   treat ensemble unanimity as high certainty.

3. Do not re-approve the same ticker repeatedly unless residual edge still
   clears a stricter bar after the market reprices.

4. If a thesis flips direction intraday on the same ticker, stop trading it.
   That is a model-stability failure, not fresh alpha.

5. Do not execute any weather trade unless the approval is persisted in the
   local recommendation trail or another explicit audit trail first.

### Signal-calibration rules

1. Downweight order-flow and market-maker confirmation on same-day weather.
   These can amplify price chasing instead of adding independent information.

2. Penalize exact-band contracts more than threshold contracts.
   A 1°F band is meaningfully harder than a one-sided `<86` or `>86`.

3. When there is partial observed locking, model the remaining intraday path
   explicitly instead of relying mainly on ensemble central tendency.

4. Require stronger justification for trades whose thesis is "market is very
   wrong" after the quote has already moved a lot intraday.

## Guidance for night mode

Night mode should mostly avoid this failure mode because it trades next-day
weather, not same-day weather. Still, the June 4 lesson should carry forward:

- avoid overconfidence on narrow weather bands
- require clean auditability from approval to execution
- treat repeated same-thesis entries as concentration, not extra edge
- rely on Kalshi fills and positions for execution truth, never on local
  summaries alone
