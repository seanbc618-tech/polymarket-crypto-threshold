"""Tests for Freqtrade-inspired leakage and recursive-feature gates."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from crypto_threshold.domain.research_integrity import (
    FeatureVector,
    IntegrityViolationKind,
    ResearchRow,
    ResearchSource,
)
from crypto_threshold.services.research_integrity_service import (
    ResearchIntegrityError,
    ResearchIntegrityService,
)

BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _rows(count: int = 12, *, spacing_minutes: int = 30) -> tuple[ResearchRow, ...]:
    result: list[ResearchRow] = []
    for index in range(count):
        decision = BASE + timedelta(minutes=index * spacing_minutes)
        target = decision + timedelta(minutes=5)
        result.append(
            ResearchRow(
                row_id=f"row-{index:02d}",
                event_group_id=f"BTC:{target.isoformat()}",
                asset="BTC",
                target_time_utc=target,
                feature_window_start=decision - timedelta(minutes=15),
                decision_at=decision,
                label_available_at=target + timedelta(minutes=1),
                inputs={"close": float(100 + index)},
                sources=(
                    ResearchSource(
                        source_id=f"source-{index:02d}",
                        role="closed_kline",
                        observed_at=decision - timedelta(seconds=1),
                        received_at=decision,
                        content_hash=f"hash-{index:02d}",
                    ),
                ),
            )
        )
    return tuple(result)


def _causal_builder(rows: tuple[ResearchRow, ...]) -> tuple[FeatureVector, ...]:
    closes: list[float] = []
    vectors: list[FeatureVector] = []
    for row in rows:
        closes.append(float(row.inputs["close"] or 0))
        vectors.append(
            FeatureVector(
                row_id=row.row_id,
                values={
                    "close": closes[-1],
                    "rolling_mean_3": sum(closes[-3:]) / len(closes[-3:]),
                },
            )
        )
    return tuple(vectors)


def _future_mean_builder(rows: tuple[ResearchRow, ...]) -> tuple[FeatureVector, ...]:
    final_mean = sum(float(row.inputs["close"] or 0) for row in rows) / len(rows)
    return tuple(
        FeatureVector(
            row_id=row.row_id,
            values={"future_mean": final_mean},
        )
        for row in rows
    )


def _recursive_builder(rows: tuple[ResearchRow, ...]) -> tuple[FeatureVector, ...]:
    value = 0.0
    vectors: list[FeatureVector] = []
    for row in rows:
        value = value * 0.9 + float(row.inputs["close"] or 0) * 0.1
        vectors.append(
            FeatureVector(row_id=row.row_id, values={"recursive_ema": value})
        )
    return tuple(vectors)


def test_causal_pipeline_passes_and_seals_reproducible_manifest() -> None:
    rows = _rows()
    service = ResearchIntegrityService()

    report = service.analyze(
        rows,
        _causal_builder,
        feature_builder_version="causal-v1",
        startup_rows=(2, 5, 11),
        max_timestamp_gap=timedelta(minutes=31),
    )
    repeated = service.analyze(
        rows,
        _causal_builder,
        feature_builder_version="causal-v1",
        startup_rows=(2, 5, 11),
        max_timestamp_gap=timedelta(minutes=31),
    )

    assert report.passed is True
    assert report.violations == ()
    assert report.lookahead_checks == 24
    assert report.recursive_checks == 6
    assert report.feature_builder_version == "causal-v1"
    assert report.max_timestamp_gap_seconds == 1860
    assert len(report.manifest_hash) == 64
    assert repeated.manifest_hash == report.manifest_hash


def test_prefix_recalculation_falsifies_a_full_frame_future_mean() -> None:
    report = ResearchIntegrityService().analyze(
        _rows(),
        _future_mean_builder,
        feature_builder_version="future-mean-v1",
        startup_rows=(2, 11),
        max_timestamp_gap=timedelta(minutes=31),
    )

    lookahead = [
        violation
        for violation in report.violations
        if violation.kind is IntegrityViolationKind.LOOKAHEAD
    ]
    assert report.passed is False
    assert len(lookahead) == 11
    assert {violation.field for violation in lookahead} == {"future_mean"}


def test_startup_window_analysis_detects_recursive_indicator_drift() -> None:
    report = ResearchIntegrityService().analyze(
        _rows(),
        _recursive_builder,
        feature_builder_version="recursive-v1",
        startup_rows=(3, 6, 11),
        numeric_tolerance=1e-12,
        max_timestamp_gap=timedelta(minutes=31),
    )

    recursive = [
        violation
        for violation in report.violations
        if violation.kind is IntegrityViolationKind.RECURSIVE_DRIFT
    ]
    assert recursive
    assert any(
        variance.startup_rows == 3 and variance.exceeds_tolerance
        for variance in report.recursive_variances
    )
    assert any(
        variance.startup_rows == 11 and not variance.exceeds_tolerance
        for variance in report.recursive_variances
    )


def test_future_payload_target_source_and_timestamp_gap_are_rejected() -> None:
    rows = list(_rows(4, spacing_minutes=1))
    future_source = replace(
        rows[1].sources[0],
        observed_at=rows[1].decision_at + timedelta(seconds=1),
        received_at=rows[1].decision_at + timedelta(seconds=2),
        target_only=True,
    )
    rows[1] = replace(rows[1], sources=(future_source,))
    rows[3] = replace(
        rows[3],
        decision_at=rows[3].decision_at + timedelta(minutes=10),
    )

    report = ResearchIntegrityService().analyze(
        tuple(rows),
        _causal_builder,
        feature_builder_version="causal-v1",
        startup_rows=(1, 3),
        max_timestamp_gap=timedelta(minutes=2),
    )
    kinds = {violation.kind for violation in report.violations}

    assert IntegrityViolationKind.FUTURE_OBSERVATION in kinds
    assert IntegrityViolationKind.FUTURE_RECEIPT in kinds
    assert IntegrityViolationKind.TARGET_LEAKAGE in kinds
    assert IntegrityViolationKind.TIMESTAMP_GAP in kinds
    assert report.passed is False


def test_non_chronological_and_malformed_feature_shapes_fail_closed() -> None:
    rows = _rows(4)

    def malformed(
        selected: tuple[ResearchRow, ...],
    ) -> tuple[FeatureVector, ...]:
        return tuple(
            FeatureVector(row_id=row.row_id, values={"bad": float("nan")})
            for row in reversed(selected)
        )

    report = ResearchIntegrityService().analyze(
        tuple(reversed(rows)),
        malformed,
        feature_builder_version="malformed-v1",
        startup_rows=(1, 3),
        max_timestamp_gap=timedelta(minutes=31),
    )
    kinds = {violation.kind for violation in report.violations}

    assert IntegrityViolationKind.NON_MONOTONIC_ROWS in kinds
    assert IntegrityViolationKind.FEATURE_SHAPE in kinds
    assert report.passed is False


def test_grouped_chronological_split_purges_and_embargoes_overlapping_events() -> None:
    rows = _rows(12, spacing_minutes=30)
    service = ResearchIntegrityService()

    split = service.chronological_split(
        rows,
        train_fraction=0.5,
        purge=timedelta(minutes=10),
        embargo=timedelta(minutes=10),
    )
    repeated = service.chronological_split(
        rows,
        train_fraction=0.5,
        purge=timedelta(minutes=10),
        embargo=timedelta(minutes=10),
    )

    assert split.train_group_ids
    assert split.test_group_ids
    assert split.excluded_group_ids
    assert not set(split.train_group_ids) & set(split.test_group_ids)
    assert repeated.manifest_hash == split.manifest_hash
    assert split.purge_seconds == 600
    assert split.embargo_seconds == 600


def test_grouped_split_refuses_mixed_group_identity_and_empty_partitions() -> None:
    rows = list(_rows(4))
    rows[1] = replace(rows[1], event_group_id=rows[0].event_group_id)

    with pytest.raises(ResearchIntegrityError, match="identity_mismatch"):
        ResearchIntegrityService().chronological_split(
            tuple(rows),
            train_fraction=0.5,
            purge=timedelta(0),
            embargo=timedelta(0),
        )

    with pytest.raises(ResearchIntegrityError, match="removed_all"):
        ResearchIntegrityService().chronological_split(
            _rows(4),
            train_fraction=0.5,
            purge=timedelta(days=10),
            embargo=timedelta(0),
        )


def test_integrity_analysis_requires_version_multiple_windows_and_gap_bound() -> None:
    service = ResearchIntegrityService()
    rows = _rows()

    with pytest.raises(ResearchIntegrityError, match="feature_builder_version"):
        service.analyze(
            rows,
            _causal_builder,
            feature_builder_version="",
            startup_rows=(2, 11),
            max_timestamp_gap=timedelta(minutes=31),
        )
    with pytest.raises(ResearchIntegrityError, match="multiple_positive"):
        service.analyze(
            rows,
            _causal_builder,
            feature_builder_version="causal-v1",
            startup_rows=(11,),
            max_timestamp_gap=timedelta(minutes=31),
        )
    with pytest.raises(ResearchIntegrityError, match="max_timestamp_gap"):
        service.analyze(
            rows,
            _causal_builder,
            feature_builder_version="causal-v1",
            startup_rows=(2, 11),
            max_timestamp_gap=timedelta(0),
        )
