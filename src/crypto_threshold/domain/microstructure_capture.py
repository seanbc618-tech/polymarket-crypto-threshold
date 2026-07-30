"""Raw public-feed and derived-feature contracts for R1 shadow collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from crypto_threshold.domain.microstructure import L2Level, TradeAggressor


class RawMicrostructureKind(StrEnum):
    SNAPSHOT = "snapshot"
    DEPTH = "depth"
    TRADE = "trade"
    PERPETUAL_MARK = "perpetual_mark"


@dataclass(frozen=True)
class RawMicrostructureEvent:
    symbol: str
    kind: RawMicrostructureKind
    exchange_at: datetime
    received_at: datetime
    source: str
    source_version: str
    payload_hash: str
    raw_payload: dict[str, Any]
    venue_sequence_start: int | None = None
    venue_sequence_end: int | None = None
    bids: tuple[L2Level, ...] = field(default_factory=tuple)
    asks: tuple[L2Level, ...] = field(default_factory=tuple)
    price: Decimal | None = None
    quantity: Decimal | None = None
    aggressor: TradeAggressor | None = None
    timestamp_trusted: bool = True


@dataclass(frozen=True)
class PerpetualMark:
    symbol: str
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal | None
    exchange_at: datetime
    received_at: datetime
    payload_hash: str
    raw_payload: dict[str, Any]
    source_version: str = "binance-usdm-premium-index-v1"


@dataclass(frozen=True)
class MicrostructureFeatureSample:
    sample_id: str
    session_id: str
    symbol: str
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
    spot_perpetual_basis_bps: Decimal | None
    btc_lead_correlation: Decimal | None
    source_event_ids: tuple[int, ...]
    source_payload_hashes: tuple[str, ...]
    source_version: str = "microstructure-features-r1-v1"


@dataclass(frozen=True)
class MicrostructureCycleResult:
    session_id: str
    persisted_events: int
    persisted_snapshots: int
    persisted_marks: int
    feature_samples: int
    integrity_runs: int
    factor_runs: int
    status: str
    reasons: tuple[str, ...]
    started_at: datetime
    completed_at: datetime
