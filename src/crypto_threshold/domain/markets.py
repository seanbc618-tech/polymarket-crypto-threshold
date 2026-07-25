"""Polymarket market and order-book domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class CryptoMarket:
    """Normalized Gamma market with explicit YES/NO token ownership."""

    market_id: str
    event_id: str | None
    condition_id: str | None
    question: str
    slug: str | None
    description: str | None
    active: bool | None
    closed: bool | None
    accepting_orders: bool | None
    enable_order_book: bool | None
    gamma_end_date: datetime | None
    outcomes: tuple[str, ...]
    yes_token_id: str | None
    no_token_id: str | None
    received_at: datetime
    raw_payload: dict[str, Any]
    event_start_time: datetime | None = None
    series_slug: str | None = None


@dataclass(frozen=True)
class OrderBookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBookSnapshot:
    """One public CLOB token book captured for an outcome."""

    market_id: str
    token_id: str
    outcome: str
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None
    spread: Decimal | None
    bid_depth: Decimal
    ask_depth: Decimal
    observed_at: datetime
    received_at: datetime
    timestamp_trusted: bool
    source_version: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class AskExecution:
    """Depth-walk result for a target USDC spend."""

    target_notional: Decimal
    filled_notional: Decimal
    shares: Decimal
    vwap: Decimal | None
    best_ask: Decimal | None
    slippage_per_share: Decimal | None
    complete: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def calculate_ask_vwap(
    asks: tuple[OrderBookLevel, ...], target_notional: Decimal
) -> AskExecution:
    """Walk executable asks from cheapest to most expensive."""
    if target_notional <= 0:
        return AskExecution(
            target_notional=target_notional,
            filled_notional=Decimal("0"),
            shares=Decimal("0"),
            vwap=None,
            best_ask=None,
            slippage_per_share=None,
            complete=False,
            reasons=("target_size_non_positive",),
        )

    levels = tuple(sorted((level for level in asks if level.price > 0), key=lambda x: x.price))
    if not levels:
        return AskExecution(
            target_notional=target_notional,
            filled_notional=Decimal("0"),
            shares=Decimal("0"),
            vwap=None,
            best_ask=None,
            slippage_per_share=None,
            complete=False,
            reasons=("empty_ask_book",),
        )

    remaining = target_notional
    filled = Decimal("0")
    shares = Decimal("0")
    for level in levels:
        available_notional = level.price * level.size
        take_notional = min(remaining, available_notional)
        filled += take_notional
        shares += take_notional / level.price
        remaining -= take_notional
        if remaining <= Decimal("0.00000001"):
            remaining = Decimal("0")
            break

    vwap = filled / shares if shares > 0 else None
    best_ask = levels[0].price
    complete = remaining == 0
    reasons = () if complete else ("insufficient_ask_depth",)
    return AskExecution(
        target_notional=target_notional,
        filled_notional=filled,
        shares=shares,
        vwap=vwap,
        best_ask=best_ask,
        slippage_per_share=(vwap - best_ask) if vwap is not None else None,
        complete=complete,
        reasons=reasons,
    )
