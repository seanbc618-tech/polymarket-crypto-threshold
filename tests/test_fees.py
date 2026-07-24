"""Per-market fee schedule tests."""

from __future__ import annotations

from decimal import Decimal

from crypto_threshold.domain.fees import (
    compute_taker_fee,
    fee_per_share,
    parse_fee_schedule,
)
from tests.conftest import NOW


def test_missing_fee_schedule_is_invalid_without_default() -> None:
    schedule = parse_fee_schedule(
        market_id="market-1",
        condition_id="condition-1",
        payload={},
        observed_at=NOW,
        received_at=NOW,
    )
    assert schedule.valid is False
    assert schedule.fee_rate is None
    assert (schedule.rejection_reason or "").startswith("missing_fee_schedule")


def test_documented_crypto_fee_curve() -> None:
    schedule = parse_fee_schedule(
        market_id="market-1",
        condition_id="condition-1",
        payload={"fd": {"r": 0.07, "e": 1, "to": True}},
        observed_at=NOW,
        received_at=NOW,
    )
    assert schedule.valid is True
    assert fee_per_share(price=Decimal("0.5"), fee_rate=schedule.fee_rate) == Decimal(
        "0.0175"
    )
    assert compute_taker_fee(
        shares=Decimal("100"), price=Decimal("0.5"), fee_rate=schedule.fee_rate
    ) == Decimal("1.75000")


def test_explicit_zero_fee_rate_is_valid() -> None:
    schedule = parse_fee_schedule(
        market_id="market-1",
        condition_id="condition-1",
        payload={"fd": {"r": 0, "e": 1, "to": True}},
        observed_at=NOW,
        received_at=NOW,
    )
    assert schedule.valid is True
    assert schedule.fee_rate == 0
