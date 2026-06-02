# AlphaGPT Framework: How Elite Quant Firms Use AI to Trade

*Source: @crptatlas, X post 2060436692377718899 — "The complete framework for running the same architecture on Polymarket today"*

---

## The Core Insight

AI does not predict markets. It compresses the time between a trading idea and a rigorous test of that idea from days to hours.

- Man Group's AlphaGPT: 20 signal ideas seriously tested per quarter → hundreds per week
- Bridgewater: $2B fund where AI makes primary trading decisions, humans oversee risk and execution
- Jane Street: $6B GPU infrastructure spend to multiply researcher throughput

**The firms winning aren't replacing their quants. They're making each quant 10x faster. Not a single AlphaGPT signal touches real capital without a researcher making a deliberate decision.**

---

## The 4-Agent Adversarial Loop (Man Group's AlphaGPT Pattern)

Run agents in this exact sequence — do not skip the challenger:

1. **Hypothesis agent** — generates a signal hypothesis with economic rationale
2. **Implementation agent** — writes the code for that signal
3. **Challenger agent** — acts as pure adversary, finds every reason the signal might be fake or overfitted
4. **Evaluator agent** — runs the backtest, decides whether to send to human review

Man Group identified the **challenger as the most valuable part**. Most systematic traders never apply adversarial review to their own hypotheses.

### Mapping to Prediction Markets

| AlphaGPT role | Prediction market equivalent |
|---|---|
| Generate hypothesis | Estimate probability from news, related markets, base rates |
| Implement | Compare estimate to current market price |
| Challenge | Ask: what would have to be true for this to be wrong? |
| Evaluate | Compute EV, send go/no-go to human |

---

## Signal Extraction from Unstructured Data

Every Fed statement, geopolitical development, or economic release contains a probability shift. Extract it with a structured prompt:

```python
POLYMARKET_SIGNAL_PROMPT = """
You are a quant analyst extracting probability signals.

Event: {event_description}
Current market price: {current_price}
New information: {news_text}

Return only a JSON object:
{
  "implied_probability_shift": float between -0.3 and 0.3,
  "confidence": float between 0.0 and 1.0,
  "key_factors": list of max 3 strings,
  "signal": one of ["strong_yes", "mild_yes", "neutral", "mild_no", "strong_no"]
}
JSON only.
"""
```

**Rules when applying this:**
- Only act on signals detected within 1 hour of the news event (stale signal = no action)
- `implied_probability_shift` must exceed fees + spread to be tradeable
- `confidence` below 0.4 is noise — do not trade

---

## Monte Carlo Significance Testing

Standard backtesting uses one path through history. One path is not enough.

If your signal does not sit in the top 5% of 10,000 random alternatives, you do not have evidence of real edge.

```python
def polymarket_edge_test(market_prices, resolutions, n=10000):
    import numpy as np

    actual_returns = []
    for price, outcome in zip(market_prices, resolutions):
        if outcome == 1:
            actual_returns.append((1 - price) / price)
        else:
            actual_returns.append(-1)

    actual_sharpe = np.mean(actual_returns) / np.std(actual_returns) * np.sqrt(252)

    random_sharpes = []
    for _ in range(n):
        s = np.random.choice(actual_returns, size=len(actual_returns), replace=True)
        random_sharpes.append(np.mean(s) / np.std(s) * np.sqrt(252))

    percentile = (np.array(random_sharpes) < actual_sharpe).mean()
    return {
        "actual_sharpe": round(actual_sharpe, 3),
        "percentile_vs_random": round(percentile, 3),
        "edge_confirmed": percentile > 0.95  # must be top 5%
    }
```

**The Polymarket advantage:** Every resolved contract is a ground truth data point. $28B traded across 9,000+ markets. Use this history aggressively.

---

## Regime-Aware Position Sizing

Do not use fixed Kelly fractions. Adjust for current market regime and drawdown:

```
f_adjusted = f_kelly × regime_factor × (1 - drawdown_factor)
```

Where:
- `f_kelly` = standard half-Kelly fraction from win probability and odds
- `regime_factor` = reduce size in high-volatility / low-liquidity regimes
- `drawdown_factor` = fraction of current drawdown from peak (if in 20% drawdown, scale by 0.8)

This maps directly to the `adaptive_sizing` logic already in `risk.py`.

---

## Deployment Health Monitoring

Three conditions that halt the system — write these down before going live:

```python
def check_system_health(live_returns, expected_sharpe, max_drawdown_limit):
    import numpy as np, pandas as pd

    r = pd.Series(live_returns)
    if len(r) < 20:
        return {"status": "insufficient data"}

    live_sharpe = r.mean() / r.std() * np.sqrt(252)
    cum = (1 + r).cumprod()
    live_dd = ((cum - cum.cummax()) / cum.cummax()).min()

    if live_dd < max_drawdown_limit:
        return {"status": "HALT - drawdown breach"}

    if (live_sharpe - expected_sharpe) / 0.3 < -2.0:
        return {"status": "HUMAN REVIEW"}

    return {"status": "operating normally", "sharpe": round(live_sharpe, 4)}
```

---

## The 6-Stage Pipeline

| Stage | Automated? | Description |
|---|---|---|
| 1. Data ingestion | Yes | Historical resolution rates, price series, correlations, volume |
| 2. Signal hypothesis | Yes | Specific, testable, with economic rationale and failure conditions |
| 3. Adversarial challenge | Yes | Separate agent breaks the hypothesis before any time is invested |
| 4. Walk-forward backtest | Yes | Every parameter estimated using only data available at trade time |
| 5. Monte Carlo significance | Yes | Must be top 5% of 10,000 random alternatives |
| 6. Human review gate | **Never automate** | Write 3 conditions that will make you stop and review the system |

### Walk-Forward Rule (Most Important)
Every parameter must be estimated using **only data available at trade time**. This single requirement eliminates the most common source of inflated backtest performance. Never use future data to fit parameters, even for normalization.

---

## Historical Data Fetch

```python
import requests
import pandas as pd

def get_polymarket_history(condition_id: str) -> pd.DataFrame:
    url = "https://clob.polymarket.com/prices-history"
    params = {"market": condition_id, "interval": "1h", "fidelity": 60}
    r = requests.get(url, params=params)
    data = r.json().get("history", [])
    df = pd.DataFrame(data)
    df["t"] = pd.to_datetime(df["t"], unit="s")
    return df.set_index("t")
```

---

## The Compression Argument (Why This Matters for Prediction Markets)

> For Polymarket specifically, the compression is even more valuable. Markets resolve on fixed dates. The window to enter at a good price is finite. The faster you go from hypothesis to validated signal, the more opportunities you actually capture.

In a 5-day trading window with fixed resolution dates, a system that tests 12 variations of a signal and evaluates all of them (rather than picking one by intuition) will capture opportunities that a slower system will miss entirely. The edge is not better information — it is testing more ideas faster than everyone else and only acting on the ones that survive adversarial review.

---

## What AI Does and Does Not Do

**AI does:**
- Compress hypothesis → rigorous test from days to hours
- Run adversarial review you would never apply to your own ideas
- Test 12 variations simultaneously instead of picking one by intuition
- Evaluate signals that have already survived automated challenge

**AI does not:**
- Predict markets
- Replace the human judgment gate
- Produce market-beating returns on its own (Citadel CTO, Ken Griffin on record)

> "We don't want PMs offloading their human investment judgment to AI." — Citadel CTO
