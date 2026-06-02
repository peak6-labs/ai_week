# Project: Kalshi Market Scorer

## Variable Naming

**Never use abbreviated variable names.** Spell every identifier out in full, regardless of how common the abbreviation is in quantitative finance or general Python.

Bad examples and their correct replacements:
- `m` → `market`
- `s` → `scored_market`
- `t` → `ticker` or `trade`
- `r` → `response`
- `p` → `probability`
- `q` → `complement_probability`
- `b` → `yes_net_odds`
- `f_star` → `full_kelly_fraction`
- `f_half` → `half_kelly_fraction`
- `ct` → `utc_close_time`
- `dt` → `close_datetime`
- `lo` → `price_low`
- `hi` → `price_high`
- `val` → use a domain-specific name (e.g. `signal_value`, `score_value`)
- `key` → use a domain-specific name (e.g. `signal_name`)
- `mid` → `midpoint_price`
- `oi` → `open_interest`
- `vol` → `volume`
- `ob` → `orderbook`
- `sem` → `concurrency_semaphore`
- `resp` → `response` or `api_response`
- `exc` → `caught_exception` or `api_exception`
- `ts` → `timestamp_seconds`
- `cov` → `signal_coverage`
- `t0` → `start_time`
- `c` (in candle context) → `candle`
- `d` (in dict context) → use a domain-specific name (e.g. `market_dict`)
- `v` → use a domain-specific name (e.g. `candle_list`)
- `b`, `a` (bid/ask values) → `bid_value`, `ask_value`
- `vals` → `valid_values`
- `coro_fn` → `coroutine_function`
- `et` → `event_ticker`
- `cat` → `category`

Single-letter lambda parameters in `sorted()` / `.sort()` calls are acceptable Python convention and may be left as-is (e.g. `key=lambda s: s.composite_score`).

## Referring to Markets

When referring to a specific Kalshi market ticker in a response, format it in backticks: `KXELEC-PRES-2028`. Do not attempt to construct clickable *deep* links — the full Kalshi URL is `kalshi.com/markets/<series_ticker>/<series_slug>/<event_ticker>`, and the middle slug is a human-readable string that cannot be derived from the ticker alone.

The one safe exception (verified by probing kalshi.com): `https://kalshi.com/markets/<series_ticker lowercased>` always resolves — the site redirects to the full canonical, filling in the slug and featured event itself. An event or market ticker used as a single path segment does NOT resolve. Build this link with the `kalshi_market_url(ticker)` helper in [kalshi_trader/web_links.py](kalshi_trader/web_links.py), which reduces any ticker to its series prefix; do not hand-assemble URLs elsewhere.
