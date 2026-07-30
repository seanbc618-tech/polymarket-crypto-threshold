"""Freqtrade-inspired research-integrity gates for offline feature pipelines."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, cast

from crypto_threshold.domain.research_integrity import (
    ChronologicalSplit,
    FeatureVector,
    IntegrityViolation,
    IntegrityViolationKind,
    NumericValue,
    RecursiveVariance,
    ResearchIntegrityReport,
    ResearchRow,
)

RESEARCH_INTEGRITY_SOURCE_VERSION = "freqtrade-inspired-integrity-r2-v1"
FeatureBuilder = Callable[[tuple[ResearchRow, ...]], tuple[FeatureVector, ...]]


class ResearchIntegrityError(ValueError):
    """The requested integrity analysis cannot be performed safely."""


@dataclass(frozen=True)
class _EventGroup:
    group_id: str
    asset: str
    target_time_utc: datetime
    feature_window_start: datetime
    label_available_at: datetime


class ResearchIntegrityService:
    """Falsify future-aware or startup-window-dependent feature calculations."""

    def analyze(
        self,
        rows: tuple[ResearchRow, ...],
        feature_builder: FeatureBuilder,
        *,
        feature_builder_version: str,
        startup_rows: tuple[int, ...],
        numeric_tolerance: float = 1e-9,
        max_timestamp_gap: timedelta,
    ) -> ResearchIntegrityReport:
        if not rows:
            raise ResearchIntegrityError("integrity_analysis_requires_rows")
        if not feature_builder_version.strip():
            raise ResearchIntegrityError("feature_builder_version_is_required")
        if len(startup_rows) < 2 or any(value < 1 for value in startup_rows):
            raise ResearchIntegrityError(
                "multiple_positive_startup_rows_are_required"
            )
        if tuple(sorted(set(startup_rows))) != startup_rows:
            raise ResearchIntegrityError("startup_rows_must_be_unique_and_sorted")
        if numeric_tolerance < 0 or not math.isfinite(numeric_tolerance):
            raise ResearchIntegrityError("numeric_tolerance_is_invalid")
        if max_timestamp_gap <= timedelta(0):
            raise ResearchIntegrityError("max_timestamp_gap_must_be_positive")

        ordered = tuple(sorted(rows, key=lambda row: (_utc(row.decision_at), row.row_id)))
        violations = self._input_violations(
            rows,
            ordered=ordered,
            max_timestamp_gap=max_timestamp_gap,
        )
        baseline, feature_violations = self._build_features(
            feature_builder,
            ordered,
            context="baseline",
        )
        violations.extend(feature_violations)
        lookahead_checks = 0
        recursive_checks = 0
        recursive_variances: list[RecursiveVariance] = []

        if baseline:
            baseline_by_row = {vector.row_id: vector for vector in baseline}
            for prefix_end in range(1, len(ordered) + 1):
                prefix = ordered[:prefix_end]
                candidate, errors = self._build_features(
                    feature_builder,
                    prefix,
                    context=f"lookahead_prefix_{prefix_end}",
                )
                violations.extend(errors)
                if not candidate:
                    continue
                row_id = prefix[-1].row_id
                expected = baseline_by_row.get(row_id)
                actual = candidate[-1]
                if expected is None:
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.FEATURE_SHAPE,
                            row_id=row_id,
                            field=None,
                            detail="baseline_missing_row",
                        )
                    )
                    continue
                comparisons, mismatches = _compare_vectors(
                    expected,
                    actual,
                    tolerance=numeric_tolerance,
                )
                lookahead_checks += comparisons
                violations.extend(
                    IntegrityViolation(
                        kind=IntegrityViolationKind.LOOKAHEAD,
                        row_id=row_id,
                        field=field,
                        detail=detail,
                    )
                    for field, detail in mismatches
                )

            baseline_last = baseline[-1]
            for startup in startup_rows:
                window_size = startup + 1
                if window_size > len(ordered):
                    recursive_variances.append(
                        RecursiveVariance(
                            feature="*",
                            startup_rows=startup,
                            baseline=None,
                            candidate=None,
                            relative_delta=math.inf,
                            exceeds_tolerance=True,
                        )
                    )
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.RECURSIVE_DRIFT,
                            row_id=ordered[-1].row_id,
                            field="*",
                            detail=(
                                f"startup_rows={startup}:"
                                f"insufficient_rows={len(ordered)}"
                            ),
                        )
                    )
                    recursive_checks += 1
                    continue
                window = ordered[-window_size:]
                candidate, errors = self._build_features(
                    feature_builder,
                    window,
                    context=f"recursive_startup_{startup}",
                )
                violations.extend(errors)
                if not candidate:
                    continue
                for feature in sorted(
                    set(baseline_last.values) | set(candidate[-1].values)
                ):
                    baseline_value = baseline_last.values.get(feature)
                    candidate_value = candidate[-1].values.get(feature)
                    relative_delta = _relative_delta(
                        baseline_value,
                        candidate_value,
                    )
                    exceeds = relative_delta > numeric_tolerance
                    recursive_checks += 1
                    recursive_variances.append(
                        RecursiveVariance(
                            feature=feature,
                            startup_rows=startup,
                            baseline=baseline_value,
                            candidate=candidate_value,
                            relative_delta=relative_delta,
                            exceeds_tolerance=exceeds,
                        )
                    )
                    if exceeds:
                        violations.append(
                            IntegrityViolation(
                                kind=IntegrityViolationKind.RECURSIVE_DRIFT,
                                row_id=ordered[-1].row_id,
                                field=feature,
                                detail=(
                                    f"startup_rows={startup}:"
                                    f"relative_delta={relative_delta:.12g}"
                                ),
                            )
                        )

        deduplicated = _deduplicate_violations(violations)
        manifest_hash = _hash(
            {
                "source_version": RESEARCH_INTEGRITY_SOURCE_VERSION,
                "rows": ordered,
                "feature_builder_version": feature_builder_version,
                "startup_rows": startup_rows,
                "numeric_tolerance": numeric_tolerance,
                "max_timestamp_gap_seconds": max_timestamp_gap.total_seconds(),
                "baseline": baseline,
                "violations": deduplicated,
                "recursive_variances": recursive_variances,
            }
        )
        reasons = tuple(
            sorted({violation.kind.value for violation in deduplicated})
        )
        return ResearchIntegrityReport(
            row_count=len(ordered),
            feature_builder_version=feature_builder_version,
            max_timestamp_gap_seconds=int(max_timestamp_gap.total_seconds()),
            lookahead_checks=lookahead_checks,
            recursive_checks=recursive_checks,
            violations=deduplicated,
            recursive_variances=tuple(recursive_variances),
            manifest_hash=manifest_hash,
            passed=not deduplicated,
            reasons=reasons,
        )

    def chronological_split(
        self,
        rows: tuple[ResearchRow, ...],
        *,
        train_fraction: float,
        purge: timedelta,
        embargo: timedelta,
    ) -> ChronologicalSplit:
        """Create one event-grouped holdout with explicit time exclusion zones."""

        if not rows:
            raise ResearchIntegrityError("chronological_split_requires_rows")
        if not 0 < train_fraction < 1:
            raise ResearchIntegrityError("train_fraction_must_be_between_zero_and_one")
        if purge < timedelta(0) or embargo < timedelta(0):
            raise ResearchIntegrityError("purge_and_embargo_must_be_non_negative")
        groups = self._event_groups(rows)
        if len(groups) < 3:
            raise ResearchIntegrityError(
                "chronological_split_requires_at_least_three_event_groups"
            )
        split_index = int(len(groups) * train_fraction)
        split_index = min(max(split_index, 1), len(groups) - 1)
        cutoff_at = groups[split_index].target_time_utc
        train_latest_at = cutoff_at - purge
        test_start_at = cutoff_at + embargo

        train = tuple(
            group.group_id
            for group in groups
            if group.target_time_utc < cutoff_at
            and group.label_available_at <= train_latest_at
        )
        test = tuple(
            group.group_id
            for group in groups
            if group.target_time_utc >= test_start_at
            and group.feature_window_start >= train_latest_at
        )
        included = set(train) | set(test)
        excluded = tuple(
            group.group_id for group in groups if group.group_id not in included
        )
        if not train:
            raise ResearchIntegrityError("purge_removed_all_training_groups")
        if not test:
            raise ResearchIntegrityError("embargo_removed_all_test_groups")
        if set(train) & set(test):
            raise ResearchIntegrityError("event_group_leakage_between_train_and_test")
        latest_training_label = max(
            group.label_available_at
            for group in groups
            if group.group_id in set(train)
        )
        earliest_test_feature = min(
            group.feature_window_start
            for group in groups
            if group.group_id in set(test)
        )
        if latest_training_label > earliest_test_feature:
            raise ResearchIntegrityError("purged_split_still_has_overlapping_windows")

        manifest_hash = _hash(
            {
                "source_version": RESEARCH_INTEGRITY_SOURCE_VERSION,
                "groups": groups,
                "train": train,
                "test": test,
                "excluded": excluded,
                "cutoff_at": cutoff_at,
                "test_start_at": test_start_at,
                "purge_seconds": int(purge.total_seconds()),
                "embargo_seconds": int(embargo.total_seconds()),
            }
        )
        return ChronologicalSplit(
            train_group_ids=train,
            test_group_ids=test,
            excluded_group_ids=excluded,
            cutoff_at=cutoff_at,
            test_start_at=test_start_at,
            purge_seconds=int(purge.total_seconds()),
            embargo_seconds=int(embargo.total_seconds()),
            manifest_hash=manifest_hash,
        )

    def _input_violations(
        self,
        original: tuple[ResearchRow, ...],
        *,
        ordered: tuple[ResearchRow, ...],
        max_timestamp_gap: timedelta,
    ) -> list[IntegrityViolation]:
        violations: list[IntegrityViolation] = []
        if tuple(row.row_id for row in original) != tuple(
            row.row_id for row in ordered
        ):
            violations.append(
                IntegrityViolation(
                    kind=IntegrityViolationKind.NON_MONOTONIC_ROWS,
                    row_id=None,
                    field="decision_at",
                    detail="input_rows_are_not_chronological",
                )
            )
        seen_rows: set[str] = set()
        group_identity: dict[str, tuple[str, datetime]] = {}
        for row in ordered:
            try:
                for field, value in (
                    ("target_time_utc", row.target_time_utc),
                    ("feature_window_start", row.feature_window_start),
                    ("decision_at", row.decision_at),
                    ("label_available_at", row.label_available_at),
                ):
                    _require_aware(value, field=field)
            except ResearchIntegrityError as exc:
                violations.append(
                    IntegrityViolation(
                        kind=IntegrityViolationKind.FEATURE_SHAPE,
                        row_id=row.row_id,
                        field=None,
                        detail=str(exc),
                    )
                )
                continue
            if not row.row_id.strip() or row.row_id in seen_rows:
                violations.append(
                    IntegrityViolation(
                        kind=IntegrityViolationKind.DUPLICATE_ROW,
                        row_id=row.row_id or None,
                        field="row_id",
                        detail="duplicate_or_empty_row_id",
                    )
                )
            seen_rows.add(row.row_id)
            identity = (row.asset.upper(), _utc(row.target_time_utc))
            previous_identity = group_identity.setdefault(
                row.event_group_id,
                identity,
            )
            if previous_identity != identity:
                violations.append(
                    IntegrityViolation(
                        kind=IntegrityViolationKind.FEATURE_SHAPE,
                        row_id=row.row_id,
                        field="event_group_id",
                        detail="event_group_has_mixed_asset_or_target",
                    )
                )
            decision_at = _utc(row.decision_at)
            if _utc(row.feature_window_start) > decision_at:
                violations.append(
                    IntegrityViolation(
                        kind=IntegrityViolationKind.FUTURE_OBSERVATION,
                        row_id=row.row_id,
                        field="feature_window_start",
                        detail="feature_window_starts_after_decision",
                    )
                )
            if _utc(row.label_available_at) <= decision_at:
                violations.append(
                    IntegrityViolation(
                        kind=IntegrityViolationKind.TARGET_LEAKAGE,
                        row_id=row.row_id,
                        field="label_available_at",
                        detail="same_event_label_available_by_decision",
                    )
                )
            source_ids: set[str] = set()
            for source in row.sources:
                try:
                    _require_aware(source.observed_at, field="source.observed_at")
                    _require_aware(source.received_at, field="source.received_at")
                except ResearchIntegrityError as exc:
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.FEATURE_SHAPE,
                            row_id=row.row_id,
                            field=source.role,
                            detail=str(exc),
                        )
                    )
                    continue
                if not source.source_id.strip() or source.source_id in source_ids:
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.DUPLICATE_SOURCE,
                            row_id=row.row_id,
                            field=source.role,
                            detail="duplicate_or_empty_source_id",
                        )
                    )
                source_ids.add(source.source_id)
                if source.target_only:
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.TARGET_LEAKAGE,
                            row_id=row.row_id,
                            field=source.role,
                            detail="target_only_source_used_as_feature_input",
                        )
                    )
                if _utc(source.received_at) < _utc(source.observed_at):
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.FEATURE_SHAPE,
                            row_id=row.row_id,
                            field=source.role,
                            detail="source_received_before_observed",
                        )
                    )
                if _utc(source.observed_at) > decision_at:
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.FUTURE_OBSERVATION,
                            row_id=row.row_id,
                            field=source.role,
                            detail=(
                                f"observed_at={_utc(source.observed_at).isoformat()}"
                                f">decision_at={decision_at.isoformat()}"
                            ),
                        )
                    )
                if _utc(source.received_at) > decision_at:
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.FUTURE_RECEIPT,
                            row_id=row.row_id,
                            field=source.role,
                            detail=(
                                f"received_at={_utc(source.received_at).isoformat()}"
                                f">decision_at={decision_at.isoformat()}"
                            ),
                        )
                    )
                if not source.content_hash.strip():
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.FEATURE_SHAPE,
                            row_id=row.row_id,
                            field=source.role,
                            detail="source_content_hash_missing",
                        )
                    )

        by_asset: dict[str, list[ResearchRow]] = defaultdict(list)
        for row in ordered:
            by_asset[row.asset.upper()].append(row)
        for asset_rows in by_asset.values():
            for previous, current in zip(asset_rows, asset_rows[1:], strict=False):
                gap = _utc(current.decision_at) - _utc(previous.decision_at)
                if gap > max_timestamp_gap:
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.TIMESTAMP_GAP,
                            row_id=current.row_id,
                            field="decision_at",
                            detail=(
                                f"previous_row={previous.row_id}:"
                                f"gap_seconds={gap.total_seconds():.6f}:"
                                f"max_seconds={max_timestamp_gap.total_seconds():.6f}"
                            ),
                        )
                    )
        return violations

    @staticmethod
    def _build_features(
        feature_builder: FeatureBuilder,
        rows: tuple[ResearchRow, ...],
        *,
        context: str,
    ) -> tuple[tuple[FeatureVector, ...], list[IntegrityViolation]]:
        try:
            vectors = feature_builder(rows)
        except Exception as exc:
            return (), [
                IntegrityViolation(
                    kind=IntegrityViolationKind.FEATURE_SHAPE,
                    row_id=rows[-1].row_id if rows else None,
                    field=None,
                    detail=f"{context}:builder_error:{type(exc).__name__}:{exc}",
                )
            ]
        if not isinstance(vectors, tuple):
            return (), [
                IntegrityViolation(
                    kind=IntegrityViolationKind.FEATURE_SHAPE,
                    row_id=rows[-1].row_id if rows else None,
                    field=None,
                    detail=f"{context}:builder_must_return_tuple",
                )
            ]
        expected_ids = tuple(row.row_id for row in rows)
        actual_ids = tuple(vector.row_id for vector in vectors)
        violations: list[IntegrityViolation] = []
        if actual_ids != expected_ids:
            violations.append(
                IntegrityViolation(
                    kind=IntegrityViolationKind.FEATURE_SHAPE,
                    row_id=rows[-1].row_id if rows else None,
                    field=None,
                    detail=(
                        f"{context}:row_ids_or_length_mismatch:"
                        f"expected={expected_ids}:actual={actual_ids}"
                    ),
                )
            )
            return (), violations
        for vector in vectors:
            if not vector.values:
                violations.append(
                    IntegrityViolation(
                        kind=IntegrityViolationKind.FEATURE_SHAPE,
                        row_id=vector.row_id,
                        field=None,
                        detail=f"{context}:empty_feature_vector",
                    )
                )
            for feature, value in vector.values.items():
                if not feature.strip() or not _is_valid_numeric(value):
                    violations.append(
                        IntegrityViolation(
                            kind=IntegrityViolationKind.FEATURE_SHAPE,
                            row_id=vector.row_id,
                            field=feature or None,
                            detail=f"{context}:invalid_feature_value:{value!r}",
                        )
                    )
        return vectors, violations

    @staticmethod
    def _event_groups(rows: tuple[ResearchRow, ...]) -> tuple[_EventGroup, ...]:
        grouped: dict[str, list[ResearchRow]] = defaultdict(list)
        for row in rows:
            for field, value in (
                ("target_time_utc", row.target_time_utc),
                ("feature_window_start", row.feature_window_start),
                ("label_available_at", row.label_available_at),
            ):
                _require_aware(value, field=field)
            grouped[row.event_group_id].append(row)
        result: list[_EventGroup] = []
        for group_id, group_rows in grouped.items():
            assets = {row.asset.upper() for row in group_rows}
            targets = {_utc(row.target_time_utc) for row in group_rows}
            if len(assets) != 1 or len(targets) != 1:
                raise ResearchIntegrityError(
                    f"event_group_identity_mismatch:{group_id}"
                )
            result.append(
                _EventGroup(
                    group_id=group_id,
                    asset=next(iter(assets)),
                    target_time_utc=next(iter(targets)),
                    feature_window_start=min(
                        _utc(row.feature_window_start) for row in group_rows
                    ),
                    label_available_at=max(
                        _utc(row.label_available_at) for row in group_rows
                    ),
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda group: (group.target_time_utc, group.group_id),
            )
        )


def _compare_vectors(
    baseline: FeatureVector,
    candidate: FeatureVector,
    *,
    tolerance: float,
) -> tuple[int, list[tuple[str, str]]]:
    mismatches: list[tuple[str, str]] = []
    features = sorted(set(baseline.values) | set(candidate.values))
    for feature in features:
        expected = baseline.values.get(feature)
        actual = candidate.values.get(feature)
        delta = _relative_delta(expected, actual)
        if delta > tolerance:
            mismatches.append(
                (
                    feature,
                    f"baseline={expected!r}:prefix={actual!r}:"
                    f"relative_delta={delta:.12g}",
                )
            )
    return len(features), mismatches


def _relative_delta(left: NumericValue, right: NumericValue) -> float:
    if left is None or right is None:
        return 0.0 if left is right else math.inf
    if isinstance(left, bool) or isinstance(right, bool):
        return 0.0 if left == right else math.inf
    left_float = float(left)
    right_float = float(right)
    if not math.isfinite(left_float) or not math.isfinite(right_float):
        return 0.0 if left_float == right_float else math.inf
    scale = max(abs(left_float), abs(right_float), 1.0)
    return abs(left_float - right_float) / scale


def _is_valid_numeric(value: NumericValue) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _deduplicate_violations(
    violations: list[IntegrityViolation],
) -> tuple[IntegrityViolation, ...]:
    unique: dict[tuple[str, str | None, str | None, str], IntegrityViolation] = {}
    for violation in violations:
        key = (
            violation.kind.value,
            violation.row_id,
            violation.field,
            violation.detail,
        )
        unique[key] = violation
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (
                item[0],
                item[1] or "",
                item[2] or "",
                item[3],
            ),
        )
    )


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResearchIntegrityError(f"{field}_must_be_timezone_aware")


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _hash(value: object) -> str:
    encoded = json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical(value: object) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _canonical(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value
