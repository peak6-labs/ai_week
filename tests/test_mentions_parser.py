import pytest

from kalshi_trader.external.mentions_parser import parse_mention_title, base_rate_from_points


# --- title parsing ---------------------------------------------------------

def test_parse_quoted_phrase_in_hearing():
    result = parse_mention_title(
        "KXMENTION-POWELL-RECESSION",
        'Will Jerome Powell say "recession" in his next congressional hearing?',
    )
    assert result is not None
    assert result["phrase"] == "recession"
    assert result["station"] == "CSPAN"
    assert result["speaker"] == "Jerome Powell"


def test_parse_say_the_word_phrase():
    result = parse_mention_title(
        "TICKER",
        'Will the President say the word "shutdown" during the briefing?',
    )
    assert result is not None
    assert result["phrase"] == "shutdown"
    assert result["station"] == "CSPAN"


def test_parse_mention_verb_bare_word():
    result = parse_mention_title(
        "TICKER",
        "Will Mullin mention inflation in the Senate hearing?",
    )
    assert result is not None
    assert result["phrase"] == "inflation"
    assert result["speaker"] == "Mullin"


def test_parse_multiword_quoted_phrase():
    result = parse_mention_title(
        "TICKER",
        'Will Powell say "higher for longer" at the press conference?',
    )
    assert result is not None
    assert result["phrase"] == "higher for longer"


def test_parse_phrase_too_long_returns_none():
    result = parse_mention_title(
        "TICKER",
        'Will he say "one two three four five six" in the hearing?',
    )
    assert result is None


def test_parse_non_mention_title_returns_none():
    result = parse_mention_title("TICKER", "Will it rain in Chicago on June 4?")
    assert result is None


def test_parse_no_extractable_phrase_returns_none():
    # "say" present but nothing parseable follows.
    result = parse_mention_title("TICKER", "What will the witness say?")
    assert result is None


# --- base-rate reduction ---------------------------------------------------

def test_base_rate_empty_points():
    summary = base_rate_from_points([])
    assert summary["period_count"] == 0
    assert summary["fraction_with_mention"] == 0.0
    assert summary["mean_match_percent"] == 0.0


def test_base_rate_fraction_and_mean():
    points = [
        {"date": "20240101T120000Z", "value": 0.0},
        {"date": "20240201T120000Z", "value": 0.5},
        {"date": "20240301T120000Z", "value": 1.5},
        {"date": "20240401T120000Z", "value": 0.0},
    ]
    summary = base_rate_from_points(points)
    assert summary["period_count"] == 4
    assert summary["periods_with_mention"] == 2
    assert summary["fraction_with_mention"] == pytest.approx(0.5)
    assert summary["mean_match_percent"] == pytest.approx((0.0 + 0.5 + 1.5 + 0.0) / 4)
    assert summary["max_match_percent"] == pytest.approx(1.5)


def test_base_rate_all_zero():
    points = [{"date": "20240101T120000Z", "value": 0.0} for _ in range(5)]
    summary = base_rate_from_points(points)
    assert summary["periods_with_mention"] == 0
    assert summary["fraction_with_mention"] == 0.0
