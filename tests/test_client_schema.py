"""The async client must normalize prod-schema responses at the boundary.

These bypass __init__ (no creds/network) and stub the raw ``get`` so the test
pins only the normalization the client applies to market/orderbook reads.
"""
import asyncio

from kalshi_trader.client import KalshiClient


def _client_with_raw(raw):
    client = KalshiClient.__new__(KalshiClient)

    async def fake_get(endpoint, params=None):
        return raw

    client.get = fake_get
    return client


def test_get_market_normalizes_prod_dollar_fields():
    raw = {"market": {"ticker": "T", "yes_bid_dollars": "0.1000",
                       "yes_ask_dollars": "0.1100", "volume_24h_fp": "559.03"}}
    out = asyncio.run(_client_with_raw(raw).get_market("T"))
    market = out["market"]
    assert market["yes_bid"] == 10.0
    assert market["yes_ask"] == 11.0
    assert market["volume_24h"] == 559.03


def test_get_markets_normalizes_every_row():
    raw = {"markets": [
        {"ticker": "A", "yes_bid_dollars": "0.2600"},
        {"ticker": "B", "yes_ask_dollars": "0.9000"},
    ]}
    out = asyncio.run(_client_with_raw(raw).get_markets())
    assert out["markets"][0]["yes_bid"] == 26.0
    assert out["markets"][1]["yes_ask"] == 90.0


def test_get_orderbook_normalizes_fp_levels():
    raw = {"orderbook_fp": {"yes_dollars": [["0.0100", "1396.00"]],
                            "no_dollars": [["0.8900", "50.00"]]}}
    out = asyncio.run(_client_with_raw(raw).get_orderbook("T"))
    assert out["orderbook"]["yes"] == [[1, 1396.0]]
    assert out["orderbook"]["no"] == [[89, 50.0]]
