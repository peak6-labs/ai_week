# Non-Financial Data Sources for Kalshi Trading Signals

Research-only survey of **new, independent, non-financial** primary-source feeds we could turn
into Kalshi probability signals. Excludes anything derived from tradeable-securities/market data
(no betting odds, no Polymarket — those are already covered, or are market data we're told to skip).

Goal of a signal: take an external observable and map it to a calibrated probability for a Kalshi
binary contract, independent of Kalshi's own order book.

Each candidate is scored 1–5 on four axes:

- **Signal value** — how directly the source moves the probability of the underlying event.
- **Quality/reliability** — authoritativeness, completeness, freedom from manipulation/error.
- **Ease of access** — free official API (5) → undocumented/scraped/paid (1).
- **Categories served** — which Kalshi market families it feeds.

---

## Summary ranking

| Rank | Source | Signal | Quality | Access | Kalshi categories |
|---|---|---|---|---|---|
| 1 | **GDELT 2.0 TV API** (Internet Archive CSPAN/news captions) | 5 | 4 | 5 | Mentions, politics, science/tech |
| 2 | **Wikimedia Pageviews / Analytics API** | 4 | 5 | 5 | Entertainment, politics, science/tech, "in the news" |
| 3 | **FiveThirtyEight polling CSVs** | 5 | 4 | 5 | Elections/politics |
| 4 | **congress.gov API + GovInfo API** (hearing schedules + transcripts) | 4 | 5 | 5 | Mentions, politics |
| 5 | **Billboard Hot 100 / Spotify charts (mirrors)** | 5 | 4 | 4 | Entertainment (music) |
| 6 | **Box office (The Numbers / Box Office Mojo / IMDb bulk)** | 5 | 4 | 3 | Entertainment (film) |
| 7 | **federalreserve.gov RSS (speeches/FOMC)** | 4 | 5 | 5 | Mentions (Fed), econ-event |
| 8 | **Rotten Tomatoes / TMDB / Metacritic** | 3 | 3 | 3 | Entertainment (film/TV) |
| 9 | **Google Trends (unofficial endpoints)** | 4 | 2 | 2 | Entertainment, politics, "search interest" |
| 10 | **whitehouse.gov / American Presidency Project briefings** | 3 | 4 | 4 | Mentions (WH press) |
| 11 | **Reality-TV fan sentiment (TellyStats-style)** | 4 | 2 | 1 | Entertainment (Love Island, etc.) |

---

## 1. GDELT 2.0 TV API — broadcast/CSPAN closed-caption keyword search

The single best find for **"mentions" markets** ("Will person X say word Y in a hearing/briefing").

- **What it is:** GDELT's TV 2.0 API exposes the Internet Archive TV News Archive's full closed-caption
  stream for 150+ stations since 2009, including the entire **CSPAN / CSPAN2 / CSPAN3** archive
  (the channels that carry congressional hearings and floor proceedings). Each broadcast is sliced
  into 15-second clips; the API reports the **percent of clips matching a keyword/phrase**, with
  per-station volume timelines, word clouds, and a `TrendingTopics` mode refreshed every 15 minutes.
  Output is machine-friendly JSON/CSV.
  - API: <https://api.gdeltproject.org/api/v2/summary/summary?d=iatv>
  - TV API debut/docs: <https://blog.gdeltproject.org/gdelt-2-0-television-api-debuts/>
  - CSPAN deep-linking writeups: <https://blog.gdeltproject.org/enriching-democracy-connecting-our-nations-legislation-to-the-legislative-process-via-deep-linking-cspan/>
- **How it maps to a probability:** Two complementary uses.
  1. **Live/near-real-time mentions:** for a market like "Will Markwayne Mullin say 'Shutdown' in a
     House hearing," query CSPAN for the phrase during the hearing window; a single matching clip →
     resolve toward Yes. Supports OR groups and a `context:` proximity operator.
  2. **Historical base rate (the real edge):** before an event, query the speaker/topic's historical
     mention frequency to build a prior — e.g. across the last N hearings on this topic, the word
     "shutdown" appeared in X% of 15-second clips → calibrate the unconditional probability it gets
     said at all. This is exactly the kind of base-rate prior that beats a naive 50/50.
- **Caveats:** captions have small transcription errors; exact-string matching only ("russia" ≠
  "russian"); phrases capped at 5 words; coverage gaps per station must be checked against the
  normalization baseline / inventory JSON. Not strictly word-for-word vs the official record, so pair
  with GovInfo transcripts (source #4) for resolution-grade confirmation.
- **Cost/limits:** free, no API key, HTTP/HTTPS, iframe-embeddable. Respect polite rate limits and
  semaphore concurrency (per project API-rate-limit guidance).
- **Scores:** Signal 5 · Quality 4 · Access 5.

---

## 2. Wikimedia Pageviews / Analytics API — attention as a probability proxy

The most accessible high-quality public attention signal, useful across nearly every category.

- **What it is:** Official Wikimedia Analytics (AQS) REST API. Per-article and per-project pageviews,
  **hourly or daily granularity, back to mid-2015**, bot-filtered, JSON.
  - Getting started: <https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/getting-started.html>
  - Per-article example endpoint: `https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia.org/all-access/all-agents/<ARTICLE>/daily/<START>/<END>`
  - Concepts/limits: <https://wikitech.wikimedia.org/wiki/Analytics/Data_Lake/Traffic/Pageviews>
- **How it maps to a probability:** Pageviews are a clean, manipulation-resistant proxy for public
  attention/momentum. Use as a **feature, not a direct probability**:
  - Entertainment: surge in a film/song/celebrity article ahead of a release or chart week.
  - Politics: relative pageview share between candidates as a momentum companion to polls.
  - "In the news"/science-tech: spikes when an event becomes salient.
  Implement as a relative z-score vs the article's own trailing baseline, then feed as a soft prior.
- **Caveats:** attention ≠ outcome (high views can be controversy, not support); a lagging-to-real-time
  signal (today's data lands within ~a day; hourly is timelier). Best as a confirming/momentum feature.
- **Cost/limits:** free, no key, documented rate limits. Top-1000 and per-article endpoints available.
- **Scores:** Signal 4 · Quality 5 · Access 5.

---

## 3. FiveThirtyEight polling data — elections

The most timely free, machine-readable polling feed for US elections.

- **What it is:** 538 publishes raw individual-poll CSVs and polling averages (president, primary,
  senate, house, governor, approval, generic ballot). Files are static CSV URLs, **refreshed daily**.
  - Index: <https://github.com/fivethirtyeight/data/tree/master/polls>
  - Live files, e.g. `https://projects.fivethirtyeight.com/polls-page/data/president_polls.csv`,
    `.../generic_ballot_polls.csv`, `.../president_approval_polls.csv`
  - Daily-refresh confirmation (AWS Data Exchange mirror): <https://github.com/rearc-data/fivethirtyeight-latest-polls>
- **How it maps to a probability:** Aggregate recent polls (recency-weight + pollster-quality weight,
  538 publishes grades) → margin → win probability via a normal model around the margin with a
  forecast-error std. Directly analogous to how `weather.py` wraps a forecast in `scipy.stats.norm`.
- **Caveats:** 538 is now under ABC/Disney; monitor for any change to the public CSV feed. RealClearPolitics
  has **no API** (scrape-only) — use 538 as the primary, RCP only as a cross-check.
  - RCP scraper context: <https://pypi.org/project/realclearpolitics/>, <https://datafield.dev/learning-prediction-markets/appendices/d-data-sources.html>
- **Cost/limits:** free, plain CSV over HTTPS, no key.
- **Scores:** Signal 5 · Quality 4 · Access 5.

---

## 4. congress.gov API + GovInfo API — hearing schedules & transcripts

The authoritative primary source pair for the legislative side of "mentions" markets.

- **congress.gov API** (Library of Congress): committee-meeting endpoint returns scheduled meetings
  with `meetingStatus` (Scheduled / Canceled / Postponed / Rescheduled), chamber, committee, linked
  documents, and `hearingTranscript` references (jacket numbers).
  - Repo/docs: <https://github.com/LibraryOfCongress/api.congress.gov/>
  - Committee-meeting endpoint: <https://github.com/LibraryOfCongress/api.congress.gov/blob/main/Documentation/CommitteeMeetingEndpoint.md>
  - Free api.data.gov key; **5,000 requests/hour**; up to 250 results/page.
- **GovInfo API** (GPO): the Congressional Hearings (CHRG) collection — full hearing transcript text,
  witness lists, metadata — plus the Congressional Record (CREC). Query by collection + lastModified
  for new-content polling; RSS feeds and sitemaps for change detection.
  - Developer hub: <https://www.govinfo.gov/developers>
  - API repo: <https://github.com/usgpo/api>
  - Same api.data.gov key; higher limits (**~36,000/hr, 1,200/min, 40/sec**).
- **How it maps to a probability:**
  - **Schedule signal:** congress.gov tells us *which* hearings are happening and when — essential for
    knowing a "mentions" market's resolution window even exists, and for scoping the GDELT base-rate query.
  - **Resolution-grade text:** GovInfo CHRG/CREC transcripts are the official word-for-word record,
    ideal for confirming whether a phrase was actually said (vs. GDELT's lower-latency-but-noisier captions).
  - Combine: GovInfo historical transcripts → per-speaker/per-topic word-frequency prior; congress.gov
    schedule → which markets are active; GDELT → live in-hearing detection.
- **Caveats:** official transcripts publish with a lag (days–weeks), so for *live* resolution lean on
  GDELT/CSPAN and use GovInfo for the historical prior and after-the-fact confirmation. Apogee
  (<https://apog.ai/docs/capabilities/committee-hearing-intelligence>) offers pre-segmented GPO
  transcripts if we ever want speaker-attributed segments without building the parser.
- **Cost/limits:** free official APIs, generous limits.
- **Scores:** Signal 4 · Quality 5 · Access 5.

---

## 5. Billboard Hot 100 / Spotify charts — music-chart battle markets

- **Billboard:** no official API, but reliable daily-updated JSON mirrors of the Hot 100, Billboard 200,
  Global 200 etc., each entry carrying `rank`, `last_week`, `peak_position`, `weeks_on_chart`.
  - <https://github.com/mhollingshead/billboard-hot-100> (every chart since 1958, updated daily)
  - <https://github.com/KoreanThinker/billboard-json> (173+ charts daily)
- **Spotify:** Spotify deprecated its public chart Web API endpoints (Nov 2024) and killed CSV downloads
  (early 2024). `charts.spotify.com` Global Weekly is still anonymously reachable via an undocumented
  catalog endpoint; **country charts are auth-walled**. Charts refresh weekly (Fri 00:00 UTC).
  - Constraints documented: <https://apify.com/s-r/free-spotify-charts/api/javascript>
  - Paid track-level alternative (peak/current positions, ISRC-keyed): <https://www.spotontrack.com/api-documentation>
- **How it maps to a probability:** For "will song A outrank song B next week" or "will X hit #1,"
  current rank + week-over-week velocity + weeks-on-chart trajectory give a strong directional prior.
  Model the rank gap and its recent drift; convert to a probability of crossover by the next chart date.
- **Caveats:** Billboard depends on third-party mirrors (mirror could break/lag — verify freshness each run).
  Spotify country-level granularity is hard to get for free. These are *leading indicators* of the next
  chart, which is what most chart markets resolve on.
- **Scores:** Signal 5 · Quality 4 · Access 4.

---

## 6. Box office — film-gross markets

- **What it is:** Daily/weekend domestic grosses with rank, theater count, cumulative total.
  - The Numbers (no official API; structured mirror w/ free tier 100 credits/mo): <https://parse.bot/marketplace/1de62da4-36a3-4487-b949-907d1296773e/the-numbers-com-api>; OpusData for licensed access: <https://www.the-numbers.com/data-services>
  - Box Office Mojo unofficial Python wrapper: <https://github.com/Stink-Po/boxoffice_api>
  - IMDb bulk data box-office time-series (day/weekend/week, by area): <https://developer.imdb.com/documentation/bulk-data-documentation/data-dictionary/box-office>
- **How it maps to a probability:** "Will film X gross > $Y opening weekend" — track Thu previews +
  Fri actuals and extrapolate to the weekend multiple; map to a normal/lognormal around the projection.
  The box-office *number itself* is the non-financial observable (per the task framing).
- **Caveats:** the best feeds are scraped/licensed, not free official APIs; scraping ToS risk (The
  Numbers explicitly blocks automated scraping — prefer the licensed OpusData or the paid mirror).
  Weekend numbers settle Mon; intraday is the edge.
- **Scores:** Signal 5 · Quality 4 · Access 3.

---

## 7. federalreserve.gov RSS — Fed speech/FOMC "mentions" & econ-event markets

- **What it is:** Per-speaker RSS feeds for all sitting Board governors plus a consolidated
  `press_monetary` feed for FOMC statements/minutes; regional-bank presidents publish via their own
  RSS/HTML. Speeches, testimony, FOMC statements, Powell press-conference transcripts.
  - Working pipelines that prove feasibility: <https://github.com/dstrunin/fed-chirp>, <https://github.com/wallscreet/frb_speeches>, <https://github.com/zsun4work/fed-speech-mcp>
  - Source: <https://www.federalreserve.gov/> RSS
- **How it maps to a probability:** schedule feed tells us when an FOMC event/speech resolution window
  is open; full text supports "will Powell say 'higher for longer'/'rate cut'" mention markets and
  hawkish/dovish tone scoring. (Note: keep the *signal* the spoken text itself — non-financial —
  not any rates market reaction.)
- **Caveats:** layout/feed changes break scrapers; transcripts can lag the live event (use a live
  caption source like GDELT for same-minute resolution).
- **Scores:** Signal 4 · Quality 5 · Access 5.

---

## 8. Rotten Tomatoes / TMDB / Metacritic — film/TV critic & audience scores

- **What it is:** No first-party RT API since the old public v1.0 was retired. Access via TMDB (free
  official key) for metadata/popularity + RT/Metacritic via scrapers or third-party wrappers.
  - TMDB official: free key at themoviedb.org/settings/api
  - OMDb (free 1,000/day) returns RT score among ratings: <http://omdbapi.com/>
  - Wrappers/scrapers: <https://github.com/SilverCrocus/rotten-tomatoes-api>, <https://apify.com/crawlerbros/tmdb-rt-metacritic-scraper/api/openapi>
- **How it maps to a probability:** "Will film X be Certified Fresh / score ≥ N% on release" — pre-release
  critic embargo lifts give an early Tomatometer; partial-count scores predict the final. TMDB
  popularity/trending complements pageviews as an attention proxy.
- **Caveats:** RT/Metacritic are scrape-only (ToS + breakage risk); early scores are volatile on low
  review counts. Mid-tier value; cleanest path is TMDB (official) + OMDb for the RT number.
- **Scores:** Signal 3 · Quality 3 · Access 3.

---

## 9. Google Trends — search-interest momentum

- **What it is:** Relative search interest. An **official API is in closed alpha** (consistently-scaled
  data, daily/weekly/monthly, 1800-day window, ~2-day lag) — apply for access.
  - Official alpha: <https://developers.google.com/search/blog/2025/07/trends-api>
  - Unofficial libs (fragile): <https://pypi.org/project/pytrends-modern/>, <https://pypi.org/project/trendspyg/>
- **How it maps to a probability:** momentum/attention feature similar to Wikipedia pageviews —
  candidate search share, "will X trend," pre-release interest.
- **Caveats:** unofficial endpoints break often and aggressively rate-limit (429s even at low volume —
  see pytrends issues #596/#638); values are 0–100 relative, not absolute, and re-scale per request.
  **Wikipedia pageviews (source #2) is the strictly better free attention proxy today**; revisit Trends
  only if we get official alpha access. Must heavily cache + low concurrency if used at all.
- **Scores:** Signal 4 · Quality 2 · Access 2.

---

## 10. White House briefings / American Presidency Project — WH press "mentions"

- **What it is:** whitehouse.gov briefings-statements page + the American Presidency Project's archive
  of press-briefing and presidential-news-conference transcripts (speaker-segmented).
  - <https://www.whitehouse.gov/briefings-statements/>
  - Parser proving structured access: <https://github.com/BuzzFeedNews/whtranscripts>
- **How it maps to a probability:** same mechanics as the Fed/congress mention markets, scoped to WH
  press briefings — historical word frequency by speaker → prior; live transcript/caption → resolution.
- **Caveats:** scrape-based; APP lags; briefing schedule is irregular. Pair with GDELT for live capture.
- **Scores:** Signal 3 · Quality 4 · Access 4.

---

## 11. Reality-TV fan sentiment (Love Island et al.)

- **What it is:** Love Island and similar shows resolve eliminations/winners partly on **public app
  votes**, for which there is **no official public data feed**. The usable public signals are
  (a) social-media sentiment aggregators like TellyStats (mentions, sentiment-by-gender, sentiment-over-time)
  and (b) bookmaker odds — but odds are market data we're told to skip.
  - TellyStats methodology: <https://tellystats.com/Articles/love-island-toms-tips-and-betting-strategy-considerations>
  - Confirms Kalshi already lists these markets: <https://www.actionnetwork.com/news/love-island-season-8-elimination-odds-love-island-usa-predictions-on-kalshi>
- **How it maps to a probability:** aggregate X/TikTok/Instagram sentiment and volume per contestant →
  rank popularity → probability of survival/elimination. This overlaps heavily with our **existing
  X/Grok sentiment signal**, which is the right vehicle here rather than a new source.
- **Caveats:** no authoritative feed; vote mechanics are opaque/producer-influenced; manipulation-prone;
  TellyStats has no public API. Lowest access score. Best handled by extending the X sentiment signal,
  not by adding a dependency.
- **Scores:** Signal 4 (when it works) · Quality 2 · Access 1.

---

## Recommended build order (top 3)

1. **GDELT 2.0 TV API (CSPAN) for "mentions" markets.** Highest novelty and category fit — "mentions"
   is a distinctive Kalshi family we have no coverage for, and GDELT is a free, keyed-no, JSON API that
   delivers both the live in-hearing detection and (more importantly) the **historical word-frequency
   base rate** that produces a real edge over a naive prior. Pair at resolution time with GovInfo
   transcripts for word-for-word confirmation.

2. **Wikimedia Pageviews / Analytics API as a cross-category attention feature.** Cheapest, most reliable
   official feed of the lot, bot-filtered, hourly/daily. It won't carry a market alone, but as a
   momentum/attention prior it strengthens entertainment, politics, and "in the news" estimates — and
   it's strictly better than Google Trends (no breakage, no 429s, no relative-rescaling headache).

3. **FiveThirtyEight polling CSVs for elections.** Elections/politics is a core Kalshi category and 538
   is a free, daily, machine-readable, pollster-graded feed that maps cleanly to a win probability with
   the same `scipy.stats.norm`-around-a-margin pattern already used in `weather.py`. RCP as a scrape-only
   cross-check only.

**Why these three:** each is a *free, independent, primary/aggregator source* (no securities/market
data), each opens a Kalshi category we don't yet serve (mentions, attention-driven entertainment,
elections), and each maps to a probability with a modeling pattern we already use. Music charts (#5)
and box office (#6) are strong next picks once the entertainment side is prioritized, but both lean on
third-party mirrors/scrapers, so they rank just behind the three official/quasi-official feeds above.
