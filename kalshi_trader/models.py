from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AgentId(str, Enum):
    A1_CONDITIONAL = "A1_conditional_event"
    A2_FLOW_VOLUME = "A2_flow_volume"
    A3_CROSS_PLATFORM = "A3_cross_platform"
    A4_DATASET_EDGE = "A4_dataset_edge"
    A5_SENTIMENT_NEWS = "A5_sentiment_news"
    A6_MARKET_MAKING = "A6_market_making"
    A7_COORDINATOR = "A7_coordinator"


@dataclass
class Market:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    yes_bid: float        # cents (0-99)
    yes_ask: float        # cents (0-99)
    last_price: float     # cents (0-99)
    volume_24h: int
    open_interest: int
    category: str
    close_time: datetime
    status: str


@dataclass
class TradeIdea:
    agent_id: AgentId
    ticker: str
    side: Side
    action: OrderAction
    confidence: float     # 0.0-1.0
    market_price: float   # cents
    reasoning: str
    signal_sources: list[str]
    suggested_size_dollars: float = 0.0
    category: str = ""


@dataclass
class RiskDecision:
    approved: bool
    approved_size_dollars: float
    rejection_reason: str = ""
    fees_estimate_cents: float = 0.0


@dataclass
class OrderResult:
    order_id: str
    ticker: str
    side: Side
    action: OrderAction
    size_dollars: float
    fill_price: float
    status: str
    created_at: datetime


@dataclass
class Position:
    ticker: str
    side: Side
    quantity: int
    avg_price: float      # cents
    current_price: float  # cents
    unrealized_pnl: float # dollars
    category: str
    close_time: datetime


@dataclass
class PortfolioState:
    balance_dollars: float
    positions: list[Position] = field(default_factory=list)
    daily_realized_pnl: float = 0.0
    total_exposure_dollars: float = 0.0
    exposure_by_category: dict[str, float] = field(default_factory=dict)


@dataclass
class RankedSlate:
    ideas: list[tuple[TradeIdea, RiskDecision]]
    generated_at: datetime
    cycle_number: int
