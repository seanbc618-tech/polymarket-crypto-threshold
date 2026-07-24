"""Probability model rejection and interval tests."""

from __future__ import annotations

from decimal import Decimal

from crypto_threshold.services.probability_service import estimate_threshold_probability


def test_non_positive_deadline_is_rejected_without_probability() -> None:
    result = estimate_threshold_probability(
        spot_price=Decimal("100000"),
        threshold=Decimal("100000"),
        time_to_deadline_hours=Decimal("0"),
        realized_volatility=Decimal("0.6"),
        operator=">",
    )
    assert result.accepted is False
    assert result.rejection_reason == "time_to_deadline_non_positive"
    assert result.base_probability is None
    assert result.probability_low is None
    assert result.probability_high is None


def test_probability_interval_is_ordered() -> None:
    result = estimate_threshold_probability(
        spot_price=Decimal("105000"),
        threshold=Decimal("100000"),
        time_to_deadline_hours=Decimal("48"),
        realized_volatility=Decimal("0.6"),
        operator=">",
    )
    assert result.accepted is True
    assert result.probability_low is not None
    assert result.base_probability is not None
    assert result.probability_high is not None
    assert result.probability_low <= result.base_probability <= result.probability_high


def test_below_probability_is_complementary_direction() -> None:
    result = estimate_threshold_probability(
        spot_price=Decimal("90000"),
        threshold=Decimal("100000"),
        time_to_deadline_hours=Decimal("168"),
        realized_volatility=Decimal("0.6"),
        operator="<",
    )
    assert result.base_probability is not None
    assert result.base_probability > Decimal("0.5")


def test_missing_volatility_is_explicit_default() -> None:
    result = estimate_threshold_probability(
        spot_price=Decimal("100000"),
        threshold=Decimal("101000"),
        time_to_deadline_hours=Decimal("24"),
    )
    assert result.accepted is True
    assert result.confidence == "low"
    assert "default_volatility_used" in result.reasons


def test_invalid_price_and_operator_reject() -> None:
    bad_spot = estimate_threshold_probability(
        spot_price=Decimal("0"),
        threshold=Decimal("100"),
        time_to_deadline_hours=Decimal("1"),
    )
    bad_operator = estimate_threshold_probability(
        spot_price=Decimal("100"),
        threshold=Decimal("100"),
        time_to_deadline_hours=Decimal("1"),
        operator="touch",
    )
    assert bad_spot.rejection_reason == "spot_price_non_positive"
    assert bad_operator.rejection_reason == "unsupported_operator"
