"""Phase 2 replay, calibration, settlement, and paper-research models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SettlementLabel:
    label_id: str
    market_id: str
    target_time_utc: datetime
    provider: str
    pair: str
    candle_interval: str
    price_field: str
    exact_operator: str
    strike: Decimal
    observed_value: Decimal
    outcome_yes: bool
    payload_id: int
    observed_at: datetime
    received_at: datetime
    source_version: str = "binance-settlement-v1"
    contract_family: str = "daily_threshold"


@dataclass(frozen=True)
class ReplayBuildResult:
    dataset_id: str
    name: str
    status: str
    item_count: int
    manifest_hash: str | None
    unique_label_count: int = 0
    training_cutoff_at: datetime | None = None
    training_cutoff_label_id: str | None = None
    rejection_reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReplayPlanResult:
    contract_family: str
    requested_unique_label_count: int
    ready: bool
    eligible_item_count: int
    eligible_unique_label_count: int
    selected_unique_label_count: int
    training_cutoff_at: datetime | None = None
    training_cutoff_label_id: str | None = None
    rejection_reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReplayVerificationResult:
    dataset_id: str
    item_count: int
    verified_count: int
    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CalibrationResult:
    run_id: str
    dataset_id: str
    status: str
    sample_count: int
    evaluated_count: int
    metrics: dict[str, Any]
    rejection_reason: str | None = None


@dataclass(frozen=True)
class PaperLedgerEntry:
    entry_id: str
    signal_id: str
    market_id: str
    policy_version: str
    action: str
    outcome: str | None
    status: str
    size_usdc: Decimal
    entry_vwap: Decimal | None
    fee_per_share: Decimal | None
    shares: Decimal | None
    total_fee: Decimal | None
    net_ev: Decimal | None
    reasons: tuple[str, ...]
    observed_at: datetime
    received_at: datetime
    source_version: str = "paper-ledger-v1"


@dataclass(frozen=True)
class ShortChallengerObservation:
    """One frozen-model and market-baseline capture at a declared checkpoint."""

    observation_id: str
    signal_id: str
    market_id: str
    asset: str
    target_time_utc: datetime
    checkpoint_lead_seconds: int
    checkpoint_at: datetime
    model_version: str
    model_probability: Decimal | None
    probability_low: Decimal | None
    probability_high: Decimal | None
    market_yes_midpoint: Decimal | None
    market_no_midpoint: Decimal | None
    market_yes_ask_vwap: Decimal | None
    market_no_ask_vwap: Decimal | None
    yes_spread: Decimal | None
    no_spread: Decimal | None
    yes_bid_depth: Decimal | None
    yes_ask_depth: Decimal | None
    no_bid_depth: Decimal | None
    no_ask_depth: Decimal | None
    yes_slippage: Decimal | None
    no_slippage: Decimal | None
    target_size_usdc: Decimal
    fee_rate: Decimal | None
    selected_outcome: str | None
    model_net_ev: Decimal | None
    status: str
    reasons: tuple[str, ...]
    observed_at: datetime
    received_at: datetime
    source_version: str = "short-challenger-r0-v1"


@dataclass(frozen=True)
class ShortLatencyReplay:
    """Counterfactual paper entry using a public book sampled after fixed latency."""

    replay_id: str
    observation_id: str
    latency_ms: int
    actual_latency_ms: int
    outcome: str | None
    action: str
    status: str
    size_usdc: Decimal
    best_ask: Decimal | None
    entry_vwap: Decimal | None
    fee_per_share: Decimal | None
    shares: Decimal | None
    total_fee: Decimal | None
    net_ev: Decimal | None
    payload_id: int | None
    reasons: tuple[str, ...]
    requested_at: datetime
    sampled_at: datetime
    source_version: str = "short-latency-replay-r0-v1"


@dataclass(frozen=True)
class ShadowCycleResult:
    cycle_id: str
    status: str
    discovered_count: int
    analyzed_count: int
    paper_entered_count: int
    paper_skipped_count: int
    stream_health: dict[str, Any]
    reasons: tuple[str, ...]
    started_at: datetime
    completed_at: datetime
    source_version: str = "shadow-monitor-v1"
    contract_family: str = "daily_threshold"
