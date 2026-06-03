from kalshi_trader.link_resolver import EventInput, resolve_event_link, slug_candidates, slugify_title, validate_event_page


def test_slugify_title_removes_punctuation_and_normalizes_ampersand():
    assert slugify_title("Drake & Bad Bunny streams this week?") == "drake-and-bad-bunny-streams-this-week"


def test_slug_candidates_include_today_stripped_series_variant():
    assert slug_candidates(series_title="Highest temperature in NYC today?")[:2] == [
        "highest-temperature-in-nyc-today",
        "highest-temperature-in-nyc",
    ]


def test_validate_event_page_rejects_generic_spa_shell():
    html = "<html><head><title>Kalshi</title></head><body>KXHIGHNY-26MAY29</body></html>"
    valid, reason, title = validate_event_page(html, "KXHIGHNY-26MAY29")
    assert not valid
    assert reason == "generic page title"
    assert title == "Kalshi"


def test_validate_event_page_requires_embedded_event_ticker():
    html = "<html><head><title>Highest temperature in NYC on May 29, 2026?</title></head></html>"
    valid, reason, _ = validate_event_page(html, "KXHIGHNY-26MAY29")
    assert not valid
    assert reason == "event ticker not embedded in page"


def test_resolve_event_link_tries_until_valid():
    event = EventInput(
        ticker="KXHIGHNY-26MAY29-T80",
        event_ticker="KXHIGHNY-26MAY29",
        series_ticker="KXHIGHNY",
        title="Highest temperature in NYC on May 29, 2026?",
        series_title="Highest temperature in NYC today?",
    )
    calls = []

    def fake_fetch(url: str) -> str:
        calls.append(url)
        if "highest-temperature-in-nyc-today" in url:
            return "<html><head><title>Kalshi</title></head></html>"
        return "<html><head><title>Highest temperature in NYC on May 29, 2026?</title></head><body>KXHIGHNY-26MAY29</body></html>"

    result = resolve_event_link(event, fetch_html=fake_fetch)

    assert result is not None
    assert result.series_slug == "highest-temperature-in-nyc"
    assert result.url == "https://kalshi.com/markets/kxhighny/highest-temperature-in-nyc/kxhighny-26may29"
    assert len(calls) == 2
