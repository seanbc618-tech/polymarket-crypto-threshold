"""Domain contracts for leakage, recursion, and chronological-split checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

NumericValue: TypeAlias = int | float | bool | None


class IntegrityViolationKind(StrEnum):
    FUTURE_OBSERVATION = "future_observation"
    FUTURE_RECEIPT = "future_receipt"
    TARGET_LEAKAGE = "target_leakage"
    TIMESTAMP_GAP = "timestamp_gap"
    DUPLICATE_ROW = "duplicate_row"
    DUPLICATE_SOURCE = "duplicate_source"
    NON_MONOTONIC_ROWS = "non_monotonic_rows"
    FEATURE_SHAPE = "feature_shape"
    LOOKAHEAD = "lookahead"
    RECURSIVE_DRIFT = "recursive_drift"


@dataclass(frozen=True)
class ResearchSource:
    """One raw payload used to make a decision."""

    source_id: str
    role: str
    observed_at: datetime
    received_at: datetime
    content_hash: str
    target_only: bool = False


@dataclass(frozen=True)
class ResearchRow:
    """A decision row grouped by one independent market event."""

    row_id: str
    event_group_id: str
    asset: str
    target_time_utc: datetime
    feature_window_start: datetime
    decision_at: datetime
    label_available_at: datetime
    inputs: dict[str, NumericValue]
    sources: tuple[ResearchSource, ...]


@dataclass(frozen=True)
class FeatureVector:
    row_id: str
    values: dict[str, NumericValue]


@dataclass(frozen=True)
class IntegrityViolation:
    kind: IntegrityViolationKind
    row_id: str | None
    field: str | None
    detail: str


@dataclass(frozen=True)
class RecursiveVariance:
    feature: str
    startup_rows: int
    baseline: NumericValue
    candidate: NumericValue
    relative_delta: float
    exceeds_tolerance: bool


@dataclass(frozen=True)
class ChronologicalSplit:
    train_group_ids: tuple[str, ...]
    test_group_ids: tuple[str, ...]
    excluded_group_ids: tuple[str, ...]
    cutoff_at: datetime
    test_start_at: datetime
    purge_seconds: int
    embargo_seconds: int
    manifest_hash: str


@dataclass(frozen=True)
class ResearchIntegrityReport:
    row_count: int
    feature_builder_version: str
    max_timestamp_gap_seconds: int
    lookahead_checks: int
    recursive_checks: int
    violations: tuple[IntegrityViolation, ...]
    recursive_variances: tuple[RecursiveVariance, ...]
    manifest_hash: str
    passed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    source_version: str = "freqtrade-inspired-integrity-r2-v1"


@dataclass(frozen=True)
class DryRunIsolationReport:
    """Explicit proof that a research runner has no authenticated mutation path."""

    trading_disabled: bool
    credentials_absent: bool
    authenticated_channel_disabled: bool
    mutation_surface_absent: bool
    passed: bool
    reasons: tuple[str, ...]
    manifest_hash: str
    source_version: str = "freqtrade-inspired-dry-run-isolation-r2-v1"
