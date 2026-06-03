"""Trade executor — DISABLED for this session (no real execution allowed).

The user has explicitly disabled all order execution for this session. The
TradeExecutor below is commented out and replaced with a guard that raises if
anything attempts to construct or use it, so no order can ever be placed by
accident. Do NOT re-enable until the user says it is okay (asked for "in the
morning"). To restore: delete the guard class and uncomment the original below.
"""
from __future__ import annotations


class TradeExecutor:  # noqa: D401 — disabled guard, not the real executor
    """Hard-disabled stand-in. Any use raises — no orders can be placed."""

    _DISABLED_MESSAGE = (
        "TradeExecutor is disabled for this session — no real execution is "
        "permitted. The pipeline is paper/mark-to-market only."
    )

    def __init__(self, *args, **kwargs):
        raise RuntimeError(self._DISABLED_MESSAGE)

    async def execute(self, *args, **kwargs):
        raise RuntimeError(self._DISABLED_MESSAGE)

    async def cancel_all(self, *args, **kwargs):
        raise RuntimeError(self._DISABLED_MESSAGE)


# --- ORIGINAL IMPLEMENTATION (commented out — DO NOT re-enable this session) ---
#
# import math
# from datetime import datetime
# from kalshi_trader.models import TradeIdea, RiskDecision, OrderResult, Side
# from kalshi_trader.risk import RiskManager
#
#
# class TradeExecutor:
#     def __init__(self, client, risk: RiskManager):
#         self._client = client
#         self._risk = risk
#
#     async def execute(self, idea: TradeIdea, decision: RiskDecision) -> OrderResult:
#         if not decision.approved:
#             raise ValueError(f"Trade not approved: {decision.rejection_reason}")
#
#         price_dollars = idea.market_price / 100.0
#         count = math.floor(decision.approved_size_dollars / price_dollars)
#         if count < 1:
#             raise ValueError(
#                 f"Position too small: ${decision.approved_size_dollars:.2f} at {price_dollars:.2f}/contract"
#             )
#
#         yes_price = int(round(idea.market_price))
#         if idea.side == Side.NO:
#             yes_price = 100 - yes_price
#
#         order_response = await self._client.create_order(
#             ticker=idea.ticker,
#             action=idea.action.value,
#             side=idea.side.value,
#             count=count,
#             order_type="market",
#             yes_price=yes_price,
#         )
#         order_data = order_response.get("order", {})
#         fill_price = float(order_data.get("yes_price", yes_price))
#
#         result = OrderResult(
#             order_id=order_data.get("order_id", ""),
#             ticker=idea.ticker,
#             side=idea.side,
#             action=idea.action,
#             size_dollars=count * price_dollars,
#             fill_price=fill_price,
#             status=order_data.get("status", "unknown"),
#             created_at=datetime.utcnow(),
#         )
#
#         return result
#
#     async def cancel_all(self, tickers: list[str] | None = None) -> int:
#         orders = await self._client.get("/portfolio/orders", params={"status": "resting"})
#         cancelled = 0
#         for order in orders.get("orders", []):
#             if tickers is None or order.get("ticker") in tickers:
#                 await self._client.cancel_order(order["order_id"])
#                 cancelled += 1
#         return cancelled
