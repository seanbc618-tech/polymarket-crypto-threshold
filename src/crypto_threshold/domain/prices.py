"""Price, kline, and cross-check domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class PriceSnapshot:
    asset: str
    quote: str
    provider: str
    symbol: str
    price: Decimal
    price_kind: str
    observed_at: datetime
    received_at: datetime
    source_version: str
    raw_payload: Any


@dataclass(frozen=True)
class Kline:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class KlineSeries:
    asset: str
    quote: str
    provider: str
    symbol: str
    interval: str
    klines: tuple[Kline, ...]
    received_at: datetime
    source_version: str
    raw_payload: Any


@dataclass(frozen=True)
class PriceCrossCheck:
    asset: str
    primary_provider: str
    secondary_provider: str
    primary_price: Decimal
    secondary_price: Decimal
    relative_diff: Decimal
    ok: bool
    observed_at: datetime
    received_at: datetime
    source_version: str
    reasons: list[str] = field(default_factory=list)
