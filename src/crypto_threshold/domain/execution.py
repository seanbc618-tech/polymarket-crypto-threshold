"""Execution-domain contracts for the isolated Nautilus-inspired blueprint."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ExecutionOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ExecutionOrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionTimeInForce(StrEnum):
    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    IOC = "IOC"


class ExecutionQuantityUnit(StrEnum):
    QUOTE_NOTIONAL = "QUOTE_NOTIONAL"
    TOKEN = "TOKEN"


class ExecutionOrderStatus(StrEnum):
    INITIALIZED = "INITIALIZED"
    DENIED = "DENIED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class ExecutionEventKind(StrEnum):
    DENIED = "DENIED"
    SUBMITTED = "SUBMITTED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    CANCELED = "CANCELED"
    FILLED = "FILLED"


@dataclass(frozen=True)
class ExecutionOrderIntent:
    """Venue-neutral order intent; it contains no signature or credential."""

    client_order_id: str
    strategy_id: str
    signal_id: str
    market_id: str
    token_id: str
    outcome: str
    order_type: ExecutionOrderType
    side: ExecutionOrderSide
    time_in_force: ExecutionTimeInForce
    quantity: Decimal
    quantity_unit: ExecutionQuantityUnit
    price: Decimal
    created_at: datetime
    post_only: bool = False
    reduce_only: bool = False
    expire_at: datetime | None = None


@dataclass(frozen=True)
class UnsignedPolymarketOrderPlan:
    """Validated translation plan which is deliberately impossible to submit."""

    client_order_id: str
    market_id: str
    token_id: str
    outcome: str
    side: ExecutionOrderSide
    order_type: ExecutionOrderType
    nautilus_time_in_force: ExecutionTimeInForce
    polymarket_order_type: str
    quantity: Decimal
    quantity_unit: ExecutionQuantityUnit
    price: Decimal
    expiration_unix: int | None
    post_only: bool
    batch_eligible: bool
    intent_fingerprint: str
    reference_tag: str
    reference_commit: str
    requires_signature: bool = True
    submission_enabled: bool = False


@dataclass(frozen=True)
class ExecutionEvent:
    """One deterministic lifecycle event from a future venue adapter or replay."""

    event_id: str
    client_order_id: str
    kind: ExecutionEventKind
    occurred_at: datetime
    received_at: datetime
    venue_order_id: str | None = None
    expected_venue_order_id: str | None = None
    fill_quantity_tokens: Decimal | None = None
    fill_price: Decimal | None = None
    trade_id: str | None = None
    fill_complete: bool = False
    reconciliation: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ExecutionOrderState:
    """Event-sourced order state used only by deterministic offline tests."""

    client_order_id: str
    intent_fingerprint: str
    quantity_unit: ExecutionQuantityUnit
    requested_quantity: Decimal
    status: ExecutionOrderStatus = ExecutionOrderStatus.INITIALIZED
    venue_order_id: str | None = None
    expected_venue_order_id: str | None = None
    filled_tokens: Decimal = Decimal("0")
    filled_quote_notional: Decimal = Decimal("0")
    reconciliation_required: bool = False
    cancel_requested: bool = False
    deferred_cancel_ready: bool = False
    processed_event_ids: frozenset[str] = field(default_factory=frozenset)
    processed_trade_ids: frozenset[str] = field(default_factory=frozenset)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    reconciliation_event_count: int = 0
    last_event_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {
            ExecutionOrderStatus.DENIED,
            ExecutionOrderStatus.CANCELED,
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.REJECTED,
        }

