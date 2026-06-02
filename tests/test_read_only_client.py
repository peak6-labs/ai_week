"""The read-only guarantee: the dashboard can never place or cancel orders.

This is load-bearing because the dashboard runs against the prod (real-money)
account. The facade must expose only reads and shout on anything mutating.
"""
import asyncio

import pytest

from kalshi_trader.dashboard.read_only_client import ReadOnlyKalshiClient


class _FakeClient:
    """Stand-in underlying client; records calls, needs no credentials."""
    def __init__(self):
        self.calls = []

    async def get_balance(self):
        self.calls.append("get_balance")
        return {"balance": 50000}

    async def get_orders(self, status="resting"):
        self.calls.append(("get_orders", status))
        return {"orders": []}

    async def get(self, endpoint, params=None):
        self.calls.append(("get", endpoint))
        return {"ok": True}

    # A write method exists on the real client and the fake — the facade must
    # NOT surface it.
    async def create_order(self, **kwargs):
        raise AssertionError("create_order must never be reachable through the facade")


@pytest.mark.parametrize("forbidden", ["post", "delete", "create_order", "cancel_order",
                                       "create_batch_orders", "cancel_all"])
def test_write_methods_are_not_reachable(forbidden):
    facade = ReadOnlyKalshiClient(client=_FakeClient())
    assert not hasattr(facade, forbidden)
    with pytest.raises(AttributeError):
        getattr(facade, forbidden)


def test_unknown_attributes_fail_closed():
    facade = ReadOnlyKalshiClient(client=_FakeClient())
    with pytest.raises(AttributeError):
        getattr(facade, "totally_made_up_method")


def test_facade_is_immutable():
    facade = ReadOnlyKalshiClient(client=_FakeClient())
    with pytest.raises(AttributeError):
        facade.something = 1


def test_read_methods_pass_through():
    fake = _FakeClient()
    facade = ReadOnlyKalshiClient(client=fake)
    assert asyncio.run(facade.get_balance()) == {"balance": 50000}
    assert asyncio.run(facade.get_orders(status="resting")) == {"orders": []}
    assert asyncio.run(facade.get("/events/X", {})) == {"ok": True}
    assert ("get_orders", "resting") in fake.calls
