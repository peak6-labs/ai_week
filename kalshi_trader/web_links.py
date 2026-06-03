"""Build kalshi.com web-page links from market data.

Kalshi's API does not expose a website URL for a market. The canonical page URL
is three lowercase segments:

    https://kalshi.com/markets/<series_ticker>/<series_slug>/<event_ticker>

Segments 1 (series_ticker) and 3 (event_ticker) come straight from the ticker;
the middle slug is a human-readable string derived from the title and is NOT
exposed by the API. A bare series-ticker URL (`/markets/<series_ticker>`) always
resolves — the site redirects to the full canonical, filling in the slug and a
featured event itself — so it is the only link we can build with no extra
knowledge.

When the slug for a series IS known, the full deep link to a *specific* event
also resolves. Verified by HTTP probing of kalshi.com on 2026-06-02:
``/kxartistvs/artist-weekly-streams-versus/kxartistvs-drakevsbunny26jun04`` loads
the Drake vs Bad Bunny market, whereas the bare ``/kxartistvs`` series link
redirects to that series' featured event (Taylor Swift vs Drake) instead — a
different event. So a deep link is strictly better when we have the slug.

``SERIES_SLUGS`` holds slugs we have observed directly (read off a real
kalshi.com URL and confirmed to resolve); they cannot be derived, only recorded.
Add to it as slugs are confirmed and every event in that series automatically
upgrades from a series link to a deep link.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

KALSHI_MARKETS_BASE_URL = "https://kalshi.com/markets"
SERIES_SLUGS_PATH = Path(__file__).with_name("series_slugs.json")

# series_ticker (lowercase) -> observed series slug. Slugs are NOT derivable from
# the API; each entry here was read off a real kalshi.com URL and verified to
# resolve. Grow this as slugs are confirmed.
def load_series_slugs(path: Path | str = SERIES_SLUGS_PATH) -> dict[str, str]:
    """Load confirmed Kalshi series slugs from disk."""
    slug_path = Path(path)
    if not slug_path.exists():
        return {}
    raw = json.loads(slug_path.read_text())
    return {str(series).lower(): str(slug).strip() for series, slug in raw.items()}


def save_series_slugs(slugs: dict[str, str], path: Path | str = SERIES_SLUGS_PATH) -> None:
    """Persist confirmed Kalshi series slugs in stable sorted order."""
    clean = {series.lower(): slug.strip() for series, slug in slugs.items()}
    Path(path).write_text(json.dumps(dict(sorted(clean.items())), indent=2) + "\n")


SERIES_SLUGS: dict[str, str] = load_series_slugs()


def kalshi_market_url(ticker: str, series_slug: Optional[str] = None, *, deep_link: Optional[bool] = None) -> str:
    """Return the best kalshi.com link we can build for a Kalshi ticker.

    Accepts a series, event, or market ticker. The series prefix (the part before
    the first hyphen) always resolves on its own, so that is the fallback. When a
    slug for the series is known — passed explicitly via ``series_slug`` or found
    in ``SERIES_SLUGS`` — the full deep link to the specific event is built
    instead, using the whole ticker (lowercased) as the event segment. Pass an
    event ticker, not a market ticker, to get a correct deep link; the report and
    dashboard paths already do. A bare series ticker (no hyphen) always returns
    the series link, since it names no specific event to deep-link to.

        >>> kalshi_market_url("KXUNLISTEDSERIES-02D26")  # no slug recorded
        'https://kalshi.com/markets/kxunlistedseries'
        >>> kalshi_market_url("KXARTISTVS-DRAKEVSBUNNY26JUN04")
        'https://kalshi.com/markets/kxartistvs/artist-weekly-streams-versus/kxartistvs-drakevsbunny26jun04'
        >>> kalshi_market_url("KXFOO-BAR26", series_slug="explicit-slug")
        'https://kalshi.com/markets/kxfoo/explicit-slug/kxfoo-bar26'
        >>> kalshi_market_url("KXBTCD")
        'https://kalshi.com/markets/kxbtcd'
    """
    series_ticker = ticker.split("-", 1)[0].lower()
    resolved_slug = series_slug or SERIES_SLUGS.get(series_ticker)
    should_deep_link = deep_link if deep_link is not None else ticker.count("-") == 1
    if resolved_slug and "-" in ticker and should_deep_link:
        return f"{KALSHI_MARKETS_BASE_URL}/{series_ticker}/{resolved_slug}/{ticker.lower()}"
    return f"{KALSHI_MARKETS_BASE_URL}/{series_ticker}"
