# Event-Driven Information Leakage in Prediction Markets

## Key Findings

Information leakage in prediction markets occurs systematically before major event announcements, with empirical evidence demonstrating that a small percentage of informed traders drive price discovery and generate measurable pre-announcement price moves.

**Scale of the Problem:**
- [Prediction markets are leaking $78M annually](https://www.humaninvariant.com/blog/pm-leak) due to information asymmetries and front-running by informed traders
- [Only 3% of traders drive Polymarket's accuracy](https://www.coindesk.com/markets/2026/04/26/only-3-of-traders-drive-polymarket-s-accuracy-not-the-crowd-study-finds), not the wisdom of crowds—these informed traders consistently predict outcomes and move prices in the right direction before public information becomes available

**Documented Pre-Announcement Trading Examples:**
- In late 2025, informed accounts on Polymarket opened high-conviction positions on OpenAI and Google product releases days before public announcements, with one account predicting 22 out of 23 categories for Google's Year in Search
- In late December 2025/early January 2026, traders purchased contracts on Venezuelan President Maduro's ouster, securing $400,000+ payouts after the U.S. military intervention on January 3, 2026
- A classified military raid saw informed trading with large orders (tens of thousands of shares) placed before price movement, generating $630,000+ in collective profits when the event occurred

**Price Discovery Dynamics:**
According to recent empirical analysis, insider trades move prices 7-12 times more aggressively per dollar than typical skilled trades, though they are concentrated in specific high-impact events rather than day-to-day trading.

## Pre-Event Timing Windows (Documented Lag Sizes)

**Information Leakage Windows:**
- **Days before announcement**: Informed traders establish positions 2-7 days before public event announcements (OpenAI/Google examples)
- **Hours before announcement**: Market structure research shows order flow imbalance patterns emerge in the hours preceding formal announcements
- **Minutes to seconds**: [Polymarket now offers 5-minute Bitcoin prediction markets](https://coinmarketcap.com/academy/article/polymarket-debuts-5-minute-bitcoin-prediction-markets-with-instant-settlement) with rapid settlement, suggesting price discovery occurs at sub-minute timescales for high-frequency events

**Order Flow Patterns:**
Research on [asymmetric post-earnings announcement drift and order flow imbalance](https://www.sciencedirect.com/science/article/abs/pii/S1057521924002485) shows that:
- Buy-initiated trades significantly exceed sell-initiated trades before positive announcements
- Sell-initiated trades dominate before negative announcements
- These patterns appear measurable hours to days in advance of formal announcements

**Pre-FOMC Announcement Drift:**
[The Pre-FOMC Announcement Drift research](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr512.pdf) documents systematic return patterns in the days preceding FOMC announcements, driven by private information leakage and informed order flow.

## Hawkes Process & Trade Clustering

**Self-Exciting Nature of Trade Activity:**
[Hawkes processes in finance](https://arxiv.org/pdf/1502.04592) model the temporal clustering of trades, capturing how:
- A single order triggers reactions from algorithms and other participants
- Trade arrivals exhibit self-excitation: when one trade occurs, it increases the probability of subsequent trades
- Clusters of trades come in waves, with intensity decaying over time following self-exciting point process dynamics

**Applications to Informed Trading Detection:**
- [Hawkes processes for market event modeling](https://questdb.com/glossary/hawkes-processes-in-market-event-modeling/) can identify abnormal clustering that precedes major announcements
- [High-frequency order flow imbalance forecasting using Hawkes processes](https://arxiv.org/html/2408.03594v1) enables detection of informed flow patterns before price moves
- [Dark pool trading analysis](https://arxiv.org/pdf/1710.01452) uses Hawkes processes to identify informed trading in venues where leakage may first manifest

**Trade Clustering as Information Signal:**
The clustering behavior captured by Hawkes models directly reflects information arrival patterns:
- Bursts of clustering activity often precede significant price moves
- Order imbalance metrics derived from Hawkes models can serve as early-warning signals for informed trading
- The intensity parameter of Hawkes processes rises measurably when informed traders are active

**Volatility Puzzle Resolution:**
[Hawkes process research](https://medium.com/@ibrahimlanre1890/hawkes-processes-a-stochastic-gem-2baa7dd1ea55) addresses the "volatility puzzle"—observed market volatility exceeds what classical economic theory predicts based on fundamental news flow. This excess volatility reflects endogenous market dynamics driven by trade clustering and informed order flow.

## Implementation Notes (What's Buildable)

**Real-Time Leakage Detection Framework:**
1. **Information Leakage Score (ILS)**: Recent academic work ([ForesightFlow](https://arxiv.org/html/2605.00493v2) and [Per-Market Information Leakage research](https://arxiv.org/html/2605.02287)) develops frameworks to quantify per-market information front-loading at announcement time
   - Requires: price action, order flow, and wallet behavior tracked in real-time (before event resolution)
   - Timing: features must be computed hours/days before announcement to detect informed positioning
   - Gap: substantial lag between post-hoc identification and real-time detection

2. **Hawkes Process-Based Early Warning System:**
   - Model trade arrivals as a self-exciting point process
   - Compute intensity parameter in rolling windows (e.g., 1-hour, 4-hour windows)
   - Flag abnormal clustering when intensity exceeds historical baselines by 2-3 standard deviations
   - Cross-reference with order imbalance metrics (more buys than sells = directional signal)

3. **Wallet Behavior Analysis:**
   - Track address accumulation patterns for specific contracts (e.g., new accounts with large positions)
   - Monitor inter-market transfers suggesting coordinated informed trading across multiple markets
   - Timestamp analysis: accounts opened immediately before positioning suggest advance knowledge

4. **Order Flow Signals:**
   - Measure buy/sell order imbalance in pre-announcement windows
   - Apply [Kyle model with price-responsive traders](https://arxiv.org/pdf/2601.09872) to separate informed vs. uninformed trading
   - Quantify information content of order flow using market microstructure metrics

5. **Event Timing Precision:**
   - Polymarket's 5-minute settlement markets enable sub-minute price discovery analysis
   - Historical event studies should quantify: how many minutes/hours before announcement do prices start moving?
   - Build probability-weighted surprise metrics (realized move vs. pre-announcement implied probability)

**Regulatory and Practical Considerations:**
- [Prediction Market Regulation and Surveillance](https://www.soliduslabs.com/post/prediction-market-regulation-surveillance) discusses market monitoring frameworks
- Systems must account for [Semantic Non-Fungibility violations](https://arxiv.org/pdf/2601.01706) where similar outcomes trade at different prices (creating arbitrage from information)
- Real-time detection requires monitoring [dark pool and off-book trading venues](https://arxiv.org/pdf/1710.01452) where leakage may first occur

## Sources

**Empirical Research on Prediction Markets:**
- [Prediction markets are leaking $78M annually](https://www.humaninvariant.com/blog/pm-leak)
- [Only 3% of traders drive Polymarket's accuracy, not the crowd](https://www.coindesk.com/markets/2026/04/26/only-3-of-traders-drive-polymarket-s-accuracy-not-the-crowd-study-finds)
- [Per-Market Information Leakage and Order-Flow Skill](https://arxiv.org/html/2605.02287)
- [ForesightFlow: An Information Leakage Score Framework](https://arxiv.org/html/2605.00493v2)

**Pre-Announcement Drift and Order Flow:**
- [The Pre-FOMC Announcement Drift and Private Information](https://www.chao-ying.net/uploads/1/3/6/5/13659330/private_information_v24.pdf)
- [Explaining Pre-Announcement Market Returns](https://www.nber.org/system/files/working_papers/w25817/revisions/w25817.rev2.pdf)
- [Asymmetric post earnings announcement drift and order flow imbalance](https://www.sciencedirect.com/science/article/abs/pii/S1057521924002485)
- [Investor sentiment and the pre-FOMC announcement drift](https://www.sciencedirect.com/science/article/abs/pii/S1544612319311262)

**Hawkes Processes and Trade Clustering:**
- [Hawkes processes in finance](https://arxiv.org/pdf/1502.04592)
- [Hawkes Processes: A Stochastic Gem](https://medium.com/@ibrahimlanre1890/hawkes-processes-a-stochastic-gem-2baa7dd1ea55)
- [Comprehensive Overview of Hawkes Processes in Market Event Modeling](https://questdb.com/glossary/hawkes-processes-in-market-event-modeling/)
- [Forecasting high frequency order flow imbalance using Hawkes processes](https://arxiv.org/html/2408.03594v1)
- [Transform Analysis for Hawkes Processes in Dark Pool Trading](https://arxiv.org/pdf/1710.01452)

**Market Microstructure and Price Models:**
- [A continuous-time Kyle model with price-responsive traders](https://arxiv.org/pdf/2601.09872)
- [Semantic Non-Fungibility and Violations of the Law of One Price](https://arxiv.org/pdf/2601.01706)
- [Warp speed price moves: Jumps after earnings announcements](https://arxiv.org/pdf/2601.08962)

**Regulatory and Governance:**
- [Prediction Market Regulation: Why Surveillance Defines the Future](https://www.soliduslabs.com/post/prediction-market-regulation-surveillance)
- [CFTC Enforcement Division Issues Prediction Markets Advisory](https://www.cftc.gov/PressRoom/PressReleases/9185-26)
- [Prediction Markets and Insider Trading Law](https://www.congress.gov/crs-product/LSB11406)

**Polymarket Specific:**
- [Polymarket Debuts 5-Minute Bitcoin Prediction Markets](https://coinmarketcap.com/academy/article/polymarket-debuts-5-minute-bitcoin-prediction-markets-with-instant-settlement)
- [Polymarket unlocks $5 trillion private market for retail traders](https://www.coindesk.com/markets/2026/05/19/polymarket-unlocks-usd5-trillion-private-market-for-retail-traders-previously-reserved-for-elites)
