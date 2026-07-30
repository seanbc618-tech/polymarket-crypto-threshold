"""Pre-registered offline factor-screening contracts for R3."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class FactorComparator(StrEnum):
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"


class FactorTradeSide(StrEnum):
    YES = "YES"
    NO = "NO"


@dataclass(frozen=True)
class FactorRule:
    rule_id: str
    factor_name: str
    comparator: FactorComparator
    threshold: Decimal
    trade_side: FactorTradeSide


@dataclass(frozen=True)
class FactorExperimentSpec:
    experiment_id: str
    spec_version: str
    created_at: datetime
    training_cutoff_at: datetime
    minimum_oos_groups: int
    minimum_dates: int
    minimum_groups_per_asset: int
    required_assets: tuple[str, ...]
    stake_usdc: Decimal
    frozen_model_version: str
    market_baseline_version: str
    integrity_source_version: str
    replay_source_version: str
    rules: tuple[FactorRule, ...]
    spec_hash: str


@dataclass(frozen=True)
class FactorObservation:
    observation_id: str
    event_group_id: str
    asset: str
    target_time_utc: datetime
    decision_at: datetime
    factor_values: dict[str, Decimal]
    candidate_probability: Decimal
    market_probability: Decimal
    frozen_v4_probability: Decimal
    outcome_yes: bool
    executable_yes_price: Decimal
    executable_no_price: Decimal
    fee_rate: Decimal
    fill_ratio: Decimal
    integrity_manifest_hash: str
    replay_manifest_hash: str


@dataclass(frozen=True)
class FactorTrialResult:
    rule_id: str
    factor_name: str
    threshold: Decimal
    comparator: FactorComparator
    trade_side: FactorTradeSide
    evaluated_groups: int
    triggered_groups: int
    filled_stake_usdc: Decimal
    total_fees_usdc: Decimal
    total_pnl_usdc: Decimal
    net_ev_per_attempted_usdc: Decimal | None
    average_fill_ratio: Decimal | None
    candidate_metrics: dict[str, float]
    market_baseline_metrics: dict[str, float]
    frozen_v4_metrics: dict[str, float]
    screening_pass: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FactorScreeningReport:
    experiment_id: str
    spec_hash: str
    observation_count: int
    event_group_count: int
    date_count: int
    assets: tuple[str, ...]
    trials: tuple[FactorTrialResult, ...]
    failed_trial_count: int
    manifest_hash: str
    promotion_allowed: bool = False
    source_version: str = "vectorbt-inspired-factor-screen-r3-v1"
