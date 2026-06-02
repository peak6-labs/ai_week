# Cross-Market Arbitrage Research: Polymarket & Kalshi

## Key Findings

1. **Platform Fragmentation Creates Persistent Arbitrage:** Polymarket (crypto-native, global) and Kalshi (CFTC-regulated US) have different user bases and information flows, generating recurring price divergences on identical events. As of February 2026, Kalshi handles ~$2.7B weekly (53% market share) vs. Polymarket's ~$2.1B weekly (47%).

2. **Price Discovery Hierarchy:** Polymarket leads Kalshi in price discovery when liquidity is high. Relative liquidity, order flow imbalances, and platform-specific dynamics determine which platform moves first—Polymarket's on-chain order book reprices within seconds, while Kalshi's central limit order book has measurable lag.

3. **Arbitrage Window Duration:** Cross-platform spreads are extremely short-lived (2–7 seconds). Market makers and competing arbitrageurs close gaps in under 60 seconds, requiring automated execution systems for profitability.

4. **Conditional Probability Arbitrage:** Cross-market arbitrage also exploits pricing inconsistencies between logically related contracts. If outcome B depends on outcome A, then P(A ∪ B) should exceed P(A), but mispricings in market-implied probabilities create opportunities when this relationship breaks down.

5. **Lag-Based Signals in Short-Duration Markets:** In 5-minute Bitcoin binary options and similar short-duration products, traders exploit lag between spot price moves (Binance/Coinbase) and Polymarket repricing. Bots profit when actual probability has reached ~85% but Polymarket still shows 50/50.

## Documented Arbitrage Spreads & Profitability

### Minimum Viable Spread for Profitability
- **Gross spread required:** 1.75–2.5 cents per contract depending on price point
- **Fee structure:** Kalshi taker fees average ~1.2% of contract value; Polymarket charges 0–1.80% (depending on category)
- **Capital requirement:** Round-trip spreads must exceed combined fees (1.5–3.6% total friction is typical)

### Arbitrage Bot Performance
- Arbitrage bots dominate Polymarket with documented millions in profits
- 62% of LLM-detected conditional dependencies in correlated markets fail to generate profit when execution costs are factored in (false positives in market relationships)
- Successful arbitrage requires sub-second automated execution to compete with existing bots

### Profitability Windows
- **Economic data release windows:** 48-hour period before/after Fed, CPI, NFP releases show largest cross-platform spreads due to differential pricing by traditional finance (Kalshi) vs. crypto (Polymarket) users
- **New market launch:** Markets launch with thin order books and wide spreads, creating temporary dislocations

## Cross-Market Lag Evidence

### Quantified Lag Mechanisms
1. **Polymarket on-chain repricing:** ~0–2 seconds after external price confirms
2. **Kalshi central limit order book updating:** 5–60 seconds, depending on market maker participation
3. **Information transmission:** Faster price discovery in markets with higher liquidity and active order flow; lower liquidity markets lag by 30+ seconds

### Information Flow Hierarchy
- Spot cryptocurrency exchanges (Binance, Coinbase) → Polymarket → Kalshi (general pattern for crypto-correlated events)
- Traditional macro releases → Kalshi → Polymarket (for macroeconomic events, though modern parity has increased)
- Recent research on 2024 presidential election data: Polymarket responded faster to large directional order flow than Kalshi

### Research Evidence
A 2024 study examining common contracts on Polymarket, Kalshi, PredictIt, and Robinhood found:
- Polymarket substantially outperforms other platforms in price discovery
- Significant price disparities persist across platforms despite same underlying event
- Lag is driven by relative liquidity (not just time-of-day) and large-trade imbalances

## Correlated Market Structures

### High-Correlation Relationships (Hedging Pairs)
- "Trump wins election" ↔ "Republicans take Senate" (highly correlated; poor diversification)
- Federal rate cuts ↔ Bond market moves (economically linked)
- Sports event participants (Team A wins ↔ Team B loses, conditional on single game)

### Low-Correlation Relationships (Diversification)
- "Will the Fed cut rates?" ↔ "Will it rain in Miami on July 4?" (independent)
- US election outcomes ↔ Geopolitical events (typically uncorrelated unless direct causal link)
- Policy decisions (narrow scope) ↔ Macro indices (broad market moves)

### Professional Portfolio Structure
- Leading prediction market traders allocate across 4 uncorrelated "buckets": elections, policy decisions, macro events, geopolitical/conflict events
- Diversification is passive (spreading capital); hedging is active (offsetting risk with negatively correlated position)
- **Risk:** High-correlation portfolios collapse together during risk-off events, creating losses despite apparent diversification

### Combinatorial Arbitrage Challenges
- LLM-based semantic filtering for conditional relationships has high false positive rate (~38% of identified relationships are tradeable)
- Market-implied probabilities may rationally deviate from mathematical relationships when liquidity, sentiment, or information asymmetries introduce real economic friction
- Combinatorial arbitrage requires lead-lag detection (Granger causality) combined with semantic validation to identify true mispricings vs. economically justified deviations

## Implementation Notes

### Execution Requirements
1. **Automation:** Sub-2-second latency required to compete with existing arbitrage bots; manual trading cannot profitably exploit 2–7 second windows
2. **Capital structure:** Simultaneous entry on both legs prevents slippage loss; serial entry (buy then sell) risks price movement against position
3. **Fee accounting:** Every strategy must model round-trip fees (typically 1.5–3.6%); thin spreads may not justify execution cost
4. **Liquidity verification:** Confirm both sides have sufficient depth before committing capital; wide bid-ask spreads on one platform can wipe out theoretical arbs

### Data Pipeline
- Monitor both Polymarket and Kalshi order books for identical events
- Detect conditional relationship mispricings using semantic and causal screening
- Track lag times (Granger causality tests on price time series)
- Flag order imbalances and large trades as leading indicators of platform repricing

### Market Selection
- **Highest spread periods:** Immediately after economic data releases (CPI, Fed meetings, NFP) and during new market launches
- **Highest volume pairs:** Presidential elections, major policy outcomes, macroeconomic events (sufficient liquidity on both platforms)
- **Avoid:** Niche/low-volume markets where Kalshi or Polymarket may lack sufficient depth; thin spreads do not overcome fee friction

### Risk Management
1. Avoid highly correlated portfolio positions that all decline together in risk-off scenarios
2. Actively monitor correlation structure of holdings; even "diversified" portfolios concentrate risk if holdings move together
3. Size positions according to liquidity on both platforms (not just your capital allocation)

## Sources

- [Polymarket & Kalshi Arbitrage Opportunities 2026 - Laika Labs](https://laikalabs.ai/prediction-markets/polymarket-kalshi-arbitrage-guide)
- [Prediction Market Arbitrage Strategies: Kalshi and Polymarket - AhaSignals Laboratory](https://ahasignals.com/research/prediction-market-arbitrage-strategies/)
- [GitHub: polymarket-arbitrage bot](https://github.com/ImMike/polymarket-arbitrage)
- [Prediction Market Arbitrage Guide - AlphaScope](https://www.alphascope.app/blog/prediction-market-arbitrage-guide)
- [Polymarket Kalshi Arbitrage - Claw Arbs](https://clawarbs.com/blog/kalshi-vs-polymarket-arbitrage/)
- [Combinatorial Arbitrage in Prediction Markets: 62% Failure Rate - Navnoor Bawa](https://navnoorbawa.substack.com/p/combinatorial-arbitrage-in-prediction)
- [AI-Augmented Arbitrage in Short-Duration Markets - Jung-Hua Liu](https://medium.com/@gwrx2005/ai-augmented-arbitrage-in-short-duration-prediction-markets-live-trading-analysis-of-polymarkets-8ce1b8c5f362)
- [Arbitrage Bots Dominate Polymarket - Yahoo Finance](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- [LLM as a Risk Manager: Lead-Lag Trading in Prediction Markets](https://arxiv.org/pdf/2602.07048)
- [Price Discovery and Trading in Modern Prediction Markets - Ng, Peng, Tao, Zhou (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5331995)
- [Political Shocks and Price Discovery in Prediction Markets - 2024 Presidential Election Study](https://arxiv.org/html/2603.03152)
- [Polymarket Trading Strategies - Web3 Bitget Academy](https://web3.bitget.com/en/academy/polymarket-trading-strategies-how-to-make-money-on-polymarket)
- [Polymarket Trading Strategies - Polyguana](https://polyguana.com/learn/polymarket-trading-strategies)
- [Hedging and Correlation Trading on Polymarket - TradeSignal AI](https://tradesignal.se/polymarket/strategies/hedging-correlation)
- [Prediction Market Portfolio Diversification Strategy - Prediction Market Tools](https://www.predictionmarketstools.com/news/prediction-market-portfolio-diversification-strategy-guide)
