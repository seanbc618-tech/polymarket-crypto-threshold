"""Polymarket fee schedule and documented taker-fee calculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

FEE_QUANTUM = Decimal("0.00001")
FEE_SOURCE_VERSION = "clob-market-info-v1"


@dataclass(frozen=True)
class MarketFeeSchedule:
    market_id: str
    condition_id: str
    fee_rate: Decimal | None
    exponent: Decimal | None
    taker_only: bool | None
    valid: bool
    rejection_reason: str | None
    observed_at: datetime
    received_at: datetime
    source_version: str
    raw_payload: dict[str, Any]


def parse_fee_schedule(
    *,
    market_id: str,
    condition_id: str,
    payload: dict[str, Any],
    observed_at: datetime,
    received_at: datetime,
) -> MarketFeeSchedule:
    """Parse CLOB market-info ``fd`` without inventing a default rate."""
    fee_data = payload.get("fd") or payload.get("feeSchedule") or payload.get("fee_schedule")
    fee_data = fee_data if isinstance(fee_data, dict) else {}
    fee_rate = _decimal(_value_for_keys(fee_data, "r", "rate"))
    exponent = _decimal(_value_for_keys(fee_data, "e", "exponent"))
    taker_raw = fee_data.get("to")
    if taker_raw is None:
        taker_raw = fee_data.get("takerOnly")
    taker_only = taker_raw if isinstance(taker_raw, bool) else None

    missing = []
    if fee_rate is None:
        missing.append("fee_rate")
    if exponent is None:
        missing.append("fee_exponent")
    if taker_only is None:
        missing.append("fee_taker_only")
    reason = f"missing_fee_schedule:{','.join(missing)}" if missing else None
    return MarketFeeSchedule(
        market_id=market_id,
        condition_id=condition_id,
        fee_rate=fee_rate,
        exponent=exponent,
        taker_only=taker_only,
        valid=not missing,
        rejection_reason=reason,
        observed_at=observed_at,
        received_at=received_at,
        source_version=FEE_SOURCE_VERSION,
        raw_payload=payload,
    )


def compute_taker_fee(*, shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Return ``shares * feeRate * p * (1-p)`` rounded to 5 decimals."""
    if shares <= 0 or fee_rate <= 0 or price < 0 or price > 1:
        return Decimal("0")
    raw = shares * fee_rate * price * (Decimal("1") - price)
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def fee_per_share(*, price: Decimal, fee_rate: Decimal) -> Decimal:
    if fee_rate <= 0 or price < 0 or price > 1:
        return Decimal("0")
    return fee_rate * price * (Decimal("1") - price)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _value_for_keys(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None
