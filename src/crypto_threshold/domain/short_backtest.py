"""Read-only R0 challenger backtest report contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class WeightedProbabilityMetrics:
    """Proper scoring metrics with each independent event group weighted equally."""

    brier: float
    log_loss: float
    ece: float
    accuracy: float


@dataclass(frozen=True)
class CheckpointBacktestResult:
    """Frozen-model and market-baseline comparison at one decision checkpoint."""

    checkpoint_lead_seconds: int
    labeled_contract_count: int
    model_probability_count: int
    market_probability_count: int
    paired_contract_count: int
    paired_event_group_count: int
    paired_date_count: int
    paired_assets: tuple[str, ...]
    model_metrics: WeightedProbabilityMetrics | None
    market_baseline_metrics: WeightedProbabilityMetrics | None
    model_beats_market_brier: bool
    model_beats_market_log_loss: bool


@dataclass(frozen=True)
class ExecutionBacktestResult:
    """Fee- and latency-aware paper execution result for one replay slice."""

    checkpoint_lead_seconds: int
    latency_ms: int
    labeled_replay_count: int
    entry_action_count: int
    settled_entry_count: int
    skipped_count: int
    open_count: int
    event_group_count: int
    date_count: int
    assets: tuple[str, ...]
    wins: int
    losses: int
    attempted_stake_usdc: Decimal
    filled_stake_usdc: Decimal
    total_fees_usdc: Decimal
    total_pnl_usdc: Decimal
    entry_rate: Decimal | None
    win_rate: Decimal | None
    roi_on_filled_stake: Decimal | None
    net_ev_per_attempted_usdc: Decimal | None
    pnl_recompute_mismatch_count: int


@dataclass(frozen=True)
class BacktestCoverage:
    """Predeclared event-diversity gate; it is not a profitability gate."""

    event_group_count: int
    date_count: int
    assets: tuple[str, ...]
    asset_group_counts: dict[str, int]
    minimum_event_groups: int
    minimum_dates: int
    required_assets: tuple[str, ...]
    minimum_groups_per_asset: int
    passed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BacktestIntegrity:
    """Mechanical checks preventing obvious future-data and accounting leakage."""

    observation_after_target_count: int
    checkpoint_timestamp_mismatch_count: int
    label_available_before_observation_count: int
    invalid_model_probability_count: int
    invalid_market_probability_count: int
    replay_pnl_recompute_mismatch_count: int
    passed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ShortChallengerBacktestReport:
    """Sealed, non-promotional report generated from a frozen SQLite snapshot."""

    generated_at: datetime
    database_path: str
    database_size_bytes: int
    database_sha256: str
    model_version: str
    observation_source_version: str
    probability_contract_count: int
    replay_row_count: int
    checkpoint_results: tuple[CheckpointBacktestResult, ...]
    execution_results: tuple[ExecutionBacktestResult, ...]
    coverage: BacktestCoverage
    integrity: BacktestIntegrity
    input_manifest_hash: str
    report_manifest_hash: str
    refit_performed: bool = False
    promotion_allowed: bool = False
    live_trading_allowed: bool = False
    source_version: str = "short-challenger-backtest-r0-v1"
