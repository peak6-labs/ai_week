"""Tests for kalshi.com link construction.

Covers the series-only fallback (the only link buildable with no extra
knowledge), the slug-registry deep-link upgrade, the explicit-slug override, and
the bare-series-ticker edge case.
"""
from kalshi_trader import web_links
from kalshi_trader.web_links import kalshi_market_url


def test_unknown_series_falls_back_to_series_link():
    # A series with no recorded slug -> series landing page only. The prefix is
    # deliberately synthetic so it can never be added to SERIES_SLUGS and quietly
    # turn this back into a deep link.
    assert "kxunlistedseries" not in web_links.SERIES_SLUGS
    assert kalshi_market_url("KXUNLISTEDSERIES-02D26") == "https://kalshi.com/markets/kxunlistedseries"


def test_known_series_builds_deep_link_from_registry():
    # kxartistvs is in SERIES_SLUGS, so every event in it upgrades to a deep link.
    assert (
        kalshi_market_url("KXARTISTVS-DRAKEVSBUNNY26JUN04")
        == "https://kalshi.com/markets/kxartistvs/artist-weekly-streams-versus/kxartistvs-drakevsbunny26jun04"
    )


def test_explicit_slug_overrides_registry_and_fallback():
    assert (
        kalshi_market_url("KXFOO-BAR26", series_slug="explicit-slug")
        == "https://kalshi.com/markets/kxfoo/explicit-slug/kxfoo-bar26"
    )


def test_explicit_slug_wins_over_registry_entry():
    # A caller-supplied slug takes precedence over the recorded one.
    assert (
        kalshi_market_url("KXARTISTVS-DRAKEVSBUNNY26JUN04", series_slug="custom")
        == "https://kalshi.com/markets/kxartistvs/custom/kxartistvs-drakevsbunny26jun04"
    )


def test_bare_series_ticker_never_deep_links():
    # No hyphen means no specific event to deep-link to, even if a slug is known.
    assert kalshi_market_url("KXARTISTVS", series_slug="some-slug") == "https://kalshi.com/markets/kxartistvs"


def test_multi_part_market_ticker_uses_series_link_unless_forced():
    assert kalshi_market_url("KXHIGHNY-26JUN02-B57.5") == "https://kalshi.com/markets/kxhighny"
    assert (
        kalshi_market_url("KXHIGHNY-26JUN02-B57.5", deep_link=True)
        == "https://kalshi.com/markets/kxhighny/highest-temperature-in-nyc-today/kxhighny-26jun02-b57.5"
    )


def test_every_registered_slug_is_lowercase_and_clean():
    # Slugs go straight into a URL path; guard against stray casing/whitespace.
    for series_ticker, series_slug in web_links.SERIES_SLUGS.items():
        assert series_ticker == series_ticker.lower()
        assert series_slug == series_slug.strip()
        assert " " not in series_slug
