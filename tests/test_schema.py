"""Tests for normalizing Kalshi's prod API schema into canonical field names.

The prod API returns prices as dollar strings (``yes_bid_dollars: "0.1000"``) and
sizes/volumes as fixed-point strings (``volume_24h_fp: "559.03"``). The rest of
the codebase reads canonical cent-denominated names (``yes_bid``, ``volume_24h``,
``open_interest``) and an ``orderbook`` with ``yes``/``no`` ``[cents, size]``
levels. These tests pin the boundary adapter that bridges the two.
"""
from kalshi_trader.schema import normalize_market, normalize_orderbook


def test_normalize_market_maps_yes_bid_dollars_to_cents():
    market = {"ticker": "T", "yes_bid_dollars": "0.1000"}
    assert normalize_market(market)["yes_bid"] == 10.0


def test_normalize_market_maps_all_price_fields_to_cents():
    market = {
        "yes_bid_dollars": "0.2600",
        "yes_ask_dollars": "0.3000",
        "no_bid_dollars": "0.8900",
        "no_ask_dollars": "0.9000",
        "last_price_dollars": "0.1100",
    }
    out = normalize_market(market)
    assert out["yes_bid"] == 26.0
    assert out["yes_ask"] == 30.0
    assert out["no_bid"] == 89.0
    assert out["no_ask"] == 90.0
    assert out["last_price"] == 11.0


def test_normalize_market_maps_fixed_point_volume_and_open_interest():
    market = {"volume_24h_fp": "559.03", "volume_fp": "767.79", "open_interest_fp": "767.79"}
    out = normalize_market(market)
    assert out["volume_24h"] == 559.03
    assert out["volume"] == 767.79
    assert out["open_interest"] == 767.79


def test_normalize_market_does_not_clobber_existing_canonical_values():
    # An old-schema snapshot row already carries yes_bid; the adapter must leave it.
    market = {"yes_bid": 27.0, "yes_ask": 28.0}
    out = normalize_market(market)
    assert out["yes_bid"] == 27.0
    assert out["yes_ask"] == 28.0


def test_normalize_market_skips_missing_fields_gracefully():
    market = {"ticker": "T"}
    out = normalize_market(market)
    assert "yes_bid" not in out  # nothing to map, nothing invented


def test_normalize_orderbook_converts_fp_levels_to_cents():
    raw = {"orderbook_fp": {
        "yes_dollars": [["0.0100", "1396.00"], ["0.0200", "2034.00"]],
        "no_dollars": [["0.0100", "9513301.00"]],
    }}
    out = normalize_orderbook(raw)["orderbook"]
    assert out["yes"] == [[1, 1396.0], [2, 2034.0]]
    assert out["no"] == [[1, 9513301.0]]


def test_normalize_orderbook_passthrough_when_already_canonical():
    raw = {"orderbook": {"yes": [[27, 100]], "no": [[72, 50]]}}
    assert normalize_orderbook(raw)["orderbook"] == {"yes": [[27, 100]], "no": [[72, 50]]}
