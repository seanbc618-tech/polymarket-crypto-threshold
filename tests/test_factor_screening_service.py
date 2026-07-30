"""R3 pre-registration, OOS, and fee-adjusted factor-screen tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_threshold.domain.factor_research import (
    FactorComparator,
    FactorObservation,
    FactorRule,
    FactorTradeSide,
)
from crypto_threshold.services.factor_screening_service import (
    FactorScreeningError,
    FactorScreeningService,
)


def _spec():
    created = datetime(2026, 7, 30, 5, 0, tzinfo=UTC)
    return FactorScreeningService().seal_spec(
        experiment_id="exp:test",
        spec_version="v1",
        created_at=created,
        training_cutoff_at=created - timedelta(days=1),
        minimum_oos_groups=20,
        minimum_dates=7,
        minimum_groups_per_asset=4,
        required_assets=("BTC", "ETH", "SOL", "XRP"),
        stake_usdc=Decimal("10"),
        frozen_model_version="frozen-v4",
        market_baseline_version="market-v1",
        integrity_source_version="integrity-v1",
        replay_source_version="replay-v1",
        rules=(
            FactorRule(
                rule_id="obi",
                factor_name="book_imbalance",
                comparator=FactorComparator.GREATER_THAN,
                threshold=Decimal("0.1"),
                trade_side=FactorTradeSide.YES,
            ),
            FactorRule(
                rule_id="missing",
                factor_name="not_present",
                comparator=FactorComparator.GREATER_THAN,
                threshold=Decimal("0"),
                trade_side=FactorTradeSide.YES,
            ),
        ),
    )


def _observations() -> tuple[FactorObservation, ...]:
    cutoff = datetime(2026, 7, 29, 5, 0, tzinfo=UTC)
    rows: list[FactorObservation] = []
    assets = ("BTC", "ETH", "SOL", "XRP")
    for day in range(7):
        for asset_index, asset in enumerate(assets):
            target = cutoff + timedelta(days=day + 1, hours=asset_index)
            rows.append(
                FactorObservation(
                    observation_id=f"obs:{day}:{asset}",
                    event_group_id=f"group:{day}:{asset}",
                    asset=asset,
                    target_time_utc=target,
                    decision_at=target - timedelta(minutes=5),
                    factor_values={"book_imbalance": Decimal("0.2")},
                    candidate_probability=Decimal("0.9"),
                    market_probability=Decimal("0.5"),
                    frozen_v4_probability=Decimal("0.6"),
                    outcome_yes=True,
                    executable_yes_price=Decimal("0.4"),
                    executable_no_price=Decimal("0.6"),
                    fee_rate=Decimal("0.01"),
                    fill_ratio=Decimal("1"),
                    integrity_manifest_hash="a" * 64,
                    replay_manifest_hash="b" * 64,
                )
            )
    return tuple(rows)


def test_factor_screen_retains_pass_and_failed_trials_with_coverage() -> None:
    service = FactorScreeningService()
    report = service.screen(_spec(), _observations())

    assert report.observation_count == 28
    assert report.event_group_count == 28
    assert report.date_count == 7
    assert report.assets == ("BTC", "ETH", "SOL", "XRP")
    assert report.promotion_allowed is False
    assert report.failed_trial_count == 1
    passed, failed = report.trials
    assert passed.screening_pass is True
    assert passed.net_ev_per_attempted_usdc is not None
    assert failed.screening_pass is False
    assert "no_triggered_oos_groups" in failed.reasons


def test_factor_screen_rejects_non_oos_and_tampered_spec() -> None:
    service = FactorScreeningService()
    spec = _spec()
    tampered = spec.__class__(**{**spec.__dict__, "spec_version": "changed"})
    with pytest.raises(FactorScreeningError, match="hash_mismatch"):
        service.screen(tampered, _observations())

    first = _observations()[0]
    not_oos = first.__class__(
        **{
            **first.__dict__,
            "target_time_utc": datetime(2026, 7, 29, 4, 0, tzinfo=UTC),
        }
    )
    with pytest.raises(FactorScreeningError, match="not_oos"):
        service.screen(spec, (not_oos, *_observations()[1:]))


def test_factor_screen_rejects_unregistered_asset_and_duplicate_observation_id() -> None:
    service = FactorScreeningService()
    spec = _spec()
    rows = _observations()
    with pytest.raises(FactorScreeningError, match="asset_not_preregistered"):
        service.screen(spec, (replace(rows[0], asset="DOGE"), *rows[1:]))
    with pytest.raises(FactorScreeningError, match="duplicate_or_empty_observation"):
        service.screen(
            spec,
            (rows[0], replace(rows[1], observation_id=rows[0].observation_id), *rows[2:]),
        )
