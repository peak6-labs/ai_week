"""Build kalshi.com web-page links from market data.

Kalshi's API does not expose a website URL for a market. The canonical page URL
is three lowercase segments:

    https://kalshi.com/markets/<series_ticker>/<series_slug>/<event_ticker>

Only segment 1 (series_ticker) and segment 3 (event_ticker) come from the API;
the middle slug is a human-readable string derived from the title and is NOT
derivable from the ticker. A bare series-ticker URL (`/markets/<series_ticker>`)
always resolves — the site redirects to the full canonical, filling in the slug
and featured event itself — so that is the only link we can build reliably.
(Verified by HTTP probing of kalshi.com on 2026-06-02.)
"""
from __future__ import annotations

KALSHI_MARKETS_BASE_URL = "https://kalshi.com/markets"


def kalshi_market_url(ticker: str) -> str:
    """Return the series landing-page URL for any Kalshi ticker.

    Accepts a series, event, or market ticker. Everything is reduced to the
    series prefix (the part before the first hyphen), lowercased, because only
    ``/markets/<series_ticker>`` resolves to a real page — an event or market
    ticker used as a single path segment returns a blank app shell.

        >>> kalshi_market_url("KXHIGHNY-26JUN02-B57.5")
        'https://kalshi.com/markets/kxhighny'
        >>> kalshi_market_url("KXBTCD")
        'https://kalshi.com/markets/kxbtcd'
    """
    series_ticker = ticker.split("-", 1)[0]
    return f"{KALSHI_MARKETS_BASE_URL}/{series_ticker.lower()}"
