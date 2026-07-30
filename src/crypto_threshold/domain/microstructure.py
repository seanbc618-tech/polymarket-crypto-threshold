"""Pure domain contracts for conservative Level-2 execution replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class BookSide(StrEnum):
    BID = "bid"
    ASK = "ask"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TradeAggressor(StrEnum):
    BUY = "buy"
    SELL = "sell"


class L2EventKind(StrEnum):
    SNAPSHOT = "snapshot"
    DEPTH = "depth"
    TRADE = "trade"


class ReplayOrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class QueueModel(StrEnum):
    """Market-by-price queue assumptions ordered from safer to less conservative."""

    RISK_AVERSE = "risk_averse"
    IDENTITY_PROBABILITY = "identity_probability"
    SQUARE_PROBABILITY = "square_probability"


class FillModel(StrEnum):
    """Whether a qualifying event may produce a partial execution."""

    ALL_OR_NOTHING = "all_or_nothing"
    PARTIAL = "partial"


class LiquidityRole(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


@dataclass(frozen=True)
class L2Level:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class L2Event:
    """One exchange-time event plus the time it became visible locally."""

    event_id: str
    instrument_id: str
    sequence: int
    kind: L2EventKind
    exchange_at: datetime
    received_at: datetime
    source: str
    source_version: str
    payload_hash: str
    side: BookSide | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    aggressor: TradeAggressor | None = None
    bids: tuple[L2Level, ...] = field(default_factory=tuple)
    asks: tuple[L2Level, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LatencyProfile:
    """Constant order-entry and response latency in milliseconds."""

    name: str
    entry_ms: int
    response_ms: int


@dataclass(frozen=True)
class ReplayOrder:
    """An offline order intent; it has no venue credentials or submission method."""

    order_id: str
    strategy_version: str
    instrument_id: str
    side: OrderSide
    order_type: ReplayOrderType
    quantity: Decimal
    submitted_at: datetime
    decision_event_id: str
    limit_price: Decimal | None = None
    maker_fee_bps: Decimal = Decimal("0")
    taker_fee_bps: Decimal = Decimal("0")
    terminal_mark_price: Decimal | None = None


@dataclass(frozen=True)
class ReplayFill:
    event_id: str
    exchange_at: datetime
    received_at: datetime
    price: Decimal
    quantity: Decimal
    liquidity: LiquidityRole


@dataclass(frozen=True)
class ReplayAttribution:
    """Marked execution PnL split into execution, direction, and fees."""

    arrival_midpoint: Decimal | None
    terminal_mark_price: Decimal | None
    spread_capture: Decimal | None
    directional_component: Decimal | None
    gross_mark_pnl: Decimal | None
    fee_cost: Decimal
    net_mark_pnl: Decimal | None
    residual_inventory_quantity: Decimal


@dataclass(frozen=True)
class L2MicrostructureFeatures:
    instrument_id: str
    as_of_exchange_at: datetime
    as_of_received_at: datetime
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    spread: Decimal
    bid_depth: Decimal
    ask_depth: Decimal
    book_imbalance: Decimal
    microprice: Decimal
    vamp: Decimal
    aggressive_trade_imbalance: Decimal
    feed_latency_ms: Decimal
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class MicrostructureReplayResult:
    order_id: str
    latency_profile: str
    queue_model: QueueModel
    fill_model: FillModel
    activation_at: datetime
    acknowledgement_at: datetime
    completed_at: datetime | None
    filled_quantity: Decimal
    remaining_quantity: Decimal
    fill_ratio: Decimal
    average_fill_price: Decimal | None
    total_notional: Decimal
    queue_ahead_at_entry: Decimal | None
    queue_ahead_at_end: Decimal | None
    fills: tuple[ReplayFill, ...]
    attribution: ReplayAttribution
    status: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MicrostructureSensitivityReport:
    order_id: str
    results: tuple[MicrostructureReplayResult, ...]
    worst_case_net_mark_pnl: Decimal | None
    best_case_net_mark_pnl: Decimal | None
    minimum_fill_ratio: Decimal
    maximum_fill_ratio: Decimal
    conservative_grid_positive: bool
    optimistic_model_dependency: bool
    manifest_hash: str
    source_version: str = "hft-inspired-l2-replay-r1-v1"
