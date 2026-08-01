"""Sealed read-only scoring and execution replay for the R0 challenger."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from sqlite3 import Row
from typing import Any

from crypto_threshold.domain.short_backtest import (
    BacktestCoverage,
    BacktestIntegrity,
    CheckpointBacktestResult,
    ExecutionBacktestResult,
    ShortChallengerBacktestReport,
    WeightedProbabilityMetrics,
)
from crypto_threshold.services.short_challenger_service import (
    SHORT_CHALLENGER_SOURCE_VERSION,
    SHORT_LATENCY_SOURCE_VERSION,
)
from crypto_threshold.storage.repositories import Repository

SHORT_CHALLENGER_BACKTEST_SOURCE_VERSION = "short-challenger-backtest-r0-v1"
DEFAULT_REQUIRED_ASSETS = ("BTC", "ETH", "SOL", "XRP")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_PNL_TOLERANCE = Decimal("0.000001")


class ShortChallengerBacktestError(ValueError):
    """The frozen snapshot cannot produce an auditable R0 report."""


class ShortChallengerBacktestService:
    """Score frozen probabilities and public-book paper fills without refitting."""

    def __init__(
        self,
        repository: Repository,
        *,
        minimum_event_groups: int = 20,
        minimum_dates: int = 7,
        required_assets: tuple[str, ...] = DEFAULT_REQUIRED_ASSETS,
        minimum_groups_per_asset: int = 4,
        calibration_bins: int = 10,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if minimum_event_groups < 1 or minimum_dates < 1:
            raise ValueError("backtest coverage minimums must be positive")
        if minimum_groups_per_asset < 1 or calibration_bins < 2:
            raise ValueError("backtest asset minimum and bins must be valid")
        normalized_assets = tuple(sorted({asset.upper() for asset in required_assets}))
        if not normalized_assets or len(normalized_assets) != len(required_assets):
            raise ValueError("required assets must be non-empty and unique")
        if not repository.database.read_only:
            raise ValueError("short challenger backtest requires a read-only database")
        self.repository = repository
        self.minimum_event_groups = minimum_event_groups
        self.minimum_dates = minimum_dates
        self.required_assets = normalized_assets
        self.minimum_groups_per_asset = minimum_groups_per_asset
        self.calibration_bins = calibration_bins
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        *,
        model_version: str,
        database_sha256: str,
        observation_source_version: str = SHORT_CHALLENGER_SOURCE_VERSION,
        replay_source_version: str = SHORT_LATENCY_SOURCE_VERSION,
    ) -> ShortChallengerBacktestReport:
        """Build a deterministic report over an explicitly named frozen model."""
        if not model_version.strip():
            raise ShortChallengerBacktestError("model_version_is_required")
        if not observation_source_version.strip() or not replay_source_version.strip():
            raise ShortChallengerBacktestError("source_versions_are_required")
        if not _is_sha256(database_sha256):
            raise ShortChallengerBacktestError("database_sha256_is_required")
        probability_rows = self.repository.short_challenger_backtest_probability_rows(
            model_version=model_version,
            observation_source_version=observation_source_version,
        )
        replay_rows = self.repository.short_challenger_backtest_replay_rows(
            model_version=model_version,
            observation_source_version=observation_source_version,
            replay_source_version=replay_source_version,
        )
        if not probability_rows:
            raise ShortChallengerBacktestError(
                f"no_labeled_probability_rows:{model_version}"
            )

        checkpoint_results = self._checkpoint_results(probability_rows)
        execution_results, pnl_mismatches = self._execution_results(replay_rows)
        coverage = self._coverage(probability_rows)
        integrity = self._integrity(
            probability_rows,
            replay_pnl_recompute_mismatch_count=pnl_mismatches,
        )
        input_manifest_hash = _rows_hash(
            probability_rows,
            replay_rows,
            prefix={
                "database_sha256": database_sha256,
                "model_version": model_version,
                "observation_source_version": observation_source_version,
                "replay_source_version": replay_source_version,
            },
        )
        database_path = self.repository.database.path
        unsealed = ShortChallengerBacktestReport(
            generated_at=_utc(self.clock()),
            database_path=str(database_path),
            database_size_bytes=database_path.stat().st_size,
            database_sha256=database_sha256,
            model_version=model_version,
            observation_source_version=observation_source_version,
            replay_source_version=replay_source_version,
            probability_contract_count=len(probability_rows),
            replay_row_count=len(replay_rows),
            checkpoint_results=checkpoint_results,
            execution_results=execution_results,
            coverage=coverage,
            integrity=integrity,
            input_manifest_hash=input_manifest_hash,
            report_manifest_hash="",
        )
        report_payload = asdict(unsealed)
        report_payload.pop("generated_at")
        report_payload.pop("database_path")
        report_payload.pop("report_manifest_hash")
        return replace(
            unsealed,
            report_manifest_hash=_hash(report_payload),
        )

    def _checkpoint_results(
        self,
        rows: list[Row],
    ) -> tuple[CheckpointBacktestResult, ...]:
        checkpoints = sorted(
            {int(row["checkpoint_lead_seconds"]) for row in rows},
            reverse=True,
        )
        results: list[CheckpointBacktestResult] = []
        for checkpoint in checkpoints:
            selected = [
                row for row in rows if int(row["checkpoint_lead_seconds"]) == checkpoint
            ]
            model_count = sum(
                _probability(row["model_probability"]) is not None for row in selected
            )
            market_count = sum(
                _probability(row["market_yes_midpoint"]) is not None for row in selected
            )
            paired = [
                row
                for row in selected
                if _probability(row["model_probability"]) is not None
                and _probability(row["market_yes_midpoint"]) is not None
            ]
            model_inputs = [
                (
                    _required_probability(row["model_probability"]),
                    int(row["outcome_yes"]),
                    _event_group(row),
                )
                for row in paired
            ]
            market_inputs = [
                (
                    _required_probability(row["market_yes_midpoint"]),
                    int(row["outcome_yes"]),
                    _event_group(row),
                )
                for row in paired
            ]
            model_metrics = self._weighted_metrics(model_inputs)
            market_metrics = self._weighted_metrics(market_inputs)
            paired_groups = {_event_group(row) for row in paired}
            results.append(
                CheckpointBacktestResult(
                    checkpoint_lead_seconds=checkpoint,
                    labeled_contract_count=len(selected),
                    model_probability_count=model_count,
                    market_probability_count=market_count,
                    paired_contract_count=len(paired),
                    paired_event_group_count=len(paired_groups),
                    paired_date_count=len({_target_date(row) for row in paired}),
                    paired_assets=tuple(sorted({_asset(row) for row in paired})),
                    model_metrics=model_metrics,
                    market_baseline_metrics=market_metrics,
                    model_beats_market_brier=(
                        model_metrics is not None
                        and market_metrics is not None
                        and model_metrics.brier < market_metrics.brier
                    ),
                    model_beats_market_log_loss=(
                        model_metrics is not None
                        and market_metrics is not None
                        and model_metrics.log_loss < market_metrics.log_loss
                    ),
                )
            )
        return tuple(results)

    def _weighted_metrics(
        self,
        values: list[tuple[float, int, str]],
    ) -> WeightedProbabilityMetrics | None:
        if not values:
            return None
        group_sizes = Counter(group for _, _, group in values)
        weighted = [
            (probability, outcome, 1.0 / group_sizes[group])
            for probability, outcome, group in values
        ]
        weight_total = sum(weight for _, _, weight in weighted)
        brier = sum(
            weight * ((probability - outcome) ** 2)
            for probability, outcome, weight in weighted
        ) / weight_total
        log_loss = -sum(
            weight
            * (
                outcome * math.log(min(max(probability, 1e-12), 1 - 1e-12))
                + (1 - outcome)
                * math.log(min(max(1 - probability, 1e-12), 1 - 1e-12))
            )
            for probability, outcome, weight in weighted
        ) / weight_total
        accuracy = sum(
            weight * int((probability >= 0.5) == bool(outcome))
            for probability, outcome, weight in weighted
        ) / weight_total
        ece = 0.0
        for bucket in range(self.calibration_bins):
            members = [
                item
                for item in weighted
                if min(int(item[0] * self.calibration_bins), self.calibration_bins - 1)
                == bucket
            ]
            bucket_weight = sum(weight for _, _, weight in members)
            if bucket_weight == 0:
                continue
            confidence = sum(
                probability * weight for probability, _, weight in members
            ) / bucket_weight
            frequency = sum(outcome * weight for _, outcome, weight in members) / bucket_weight
            ece += (bucket_weight / weight_total) * abs(confidence - frequency)
        return WeightedProbabilityMetrics(
            brier=brier,
            log_loss=log_loss,
            ece=ece,
            accuracy=accuracy,
        )

    def _execution_results(
        self,
        rows: list[Row],
    ) -> tuple[tuple[ExecutionBacktestResult, ...], int]:
        slices = sorted(
            {
                (int(row["checkpoint_lead_seconds"]), int(row["latency_ms"]))
                for row in rows
            },
            key=lambda value: (-value[0], value[1]),
        )
        total_mismatches = 0
        results: list[ExecutionBacktestResult] = []
        for checkpoint, latency_ms in slices:
            selected = [
                row
                for row in rows
                if int(row["checkpoint_lead_seconds"]) == checkpoint
                and int(row["latency_ms"]) == latency_ms
            ]
            attempted_stake = sum(
                (_decimal(row["size_usdc"]) or _ZERO for row in selected),
                start=_ZERO,
            )
            entries = [row for row in selected if str(row["action"]) == "enter"]
            settled = [row for row in entries if str(row["status"]) == "settled"]
            filled_stake = _ZERO
            total_fees = _ZERO
            total_pnl = _ZERO
            wins = 0
            losses = 0
            mismatches = 0
            for row in settled:
                size = _decimal(row["size_usdc"])
                shares = _decimal(row["shares"])
                fee = _decimal(row["total_fee"])
                recorded_pnl = _decimal(row["pnl_usdc"])
                outcome = str(row["outcome"] or "")
                label = bool(row["latest_outcome_yes"])
                if (
                    size is None
                    or shares is None
                    or fee is None
                    or recorded_pnl is None
                    or outcome not in {"YES", "NO"}
                ):
                    mismatches += 1
                    continue
                won = (outcome == "YES" and label) or (outcome == "NO" and not label)
                recomputed_pnl = (shares if won else _ZERO) - size - fee
                if abs(recomputed_pnl - recorded_pnl) > _PNL_TOLERANCE:
                    mismatches += 1
                wins += int(won)
                losses += int(not won)
                filled_stake += size
                total_fees += fee
                total_pnl += recomputed_pnl
            total_mismatches += mismatches
            settled_count = len(settled)
            labeled_count = len(selected)
            results.append(
                ExecutionBacktestResult(
                    checkpoint_lead_seconds=checkpoint,
                    latency_ms=latency_ms,
                    labeled_replay_count=labeled_count,
                    entry_action_count=len(entries),
                    settled_entry_count=settled_count,
                    skipped_count=sum(str(row["status"]) == "skipped" for row in selected),
                    open_count=sum(str(row["status"]) == "open" for row in selected),
                    event_group_count=len({_event_group(row) for row in selected}),
                    date_count=len({_target_date(row) for row in selected}),
                    assets=tuple(sorted({_asset(row) for row in selected})),
                    wins=wins,
                    losses=losses,
                    attempted_stake_usdc=attempted_stake,
                    filled_stake_usdc=filled_stake,
                    total_fees_usdc=total_fees,
                    total_pnl_usdc=total_pnl,
                    entry_rate=(
                        Decimal(len(entries)) / Decimal(labeled_count)
                        if labeled_count
                        else None
                    ),
                    win_rate=(
                        Decimal(wins) / Decimal(wins + losses)
                        if wins + losses
                        else None
                    ),
                    roi_on_filled_stake=(
                        total_pnl / filled_stake if filled_stake > 0 else None
                    ),
                    net_ev_per_attempted_usdc=(
                        total_pnl / attempted_stake if attempted_stake > 0 else None
                    ),
                    pnl_recompute_mismatch_count=mismatches,
                )
            )
        return tuple(results), total_mismatches

    def _coverage(self, rows: list[Row]) -> BacktestCoverage:
        group_assets = {_event_group(row): _asset(row) for row in rows}
        group_dates = {_event_group(row): _target_date(row) for row in rows}
        asset_counts = Counter(group_assets.values())
        reasons: list[str] = []
        if len(group_assets) < self.minimum_event_groups:
            reasons.append(
                f"insufficient_event_groups:{len(group_assets)}/{self.minimum_event_groups}"
            )
        date_count = len(set(group_dates.values()))
        if date_count < self.minimum_dates:
            reasons.append(f"insufficient_dates:{date_count}/{self.minimum_dates}")
        for asset in self.required_assets:
            count = asset_counts.get(asset, 0)
            if count == 0:
                reasons.append(f"missing_required_asset:{asset}")
            elif count < self.minimum_groups_per_asset:
                reasons.append(
                    f"insufficient_asset_groups:{asset}:{count}/"
                    f"{self.minimum_groups_per_asset}"
                )
        return BacktestCoverage(
            event_group_count=len(group_assets),
            date_count=date_count,
            assets=tuple(sorted(asset_counts)),
            asset_group_counts=dict(sorted(asset_counts.items())),
            minimum_event_groups=self.minimum_event_groups,
            minimum_dates=self.minimum_dates,
            required_assets=self.required_assets,
            minimum_groups_per_asset=self.minimum_groups_per_asset,
            passed=not reasons,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _integrity(
        rows: list[Row],
        *,
        replay_pnl_recompute_mismatch_count: int,
    ) -> BacktestIntegrity:
        observation_after_target = 0
        checkpoint_mismatches = 0
        premature_labels = 0
        invalid_model = 0
        invalid_market = 0
        for row in rows:
            target = _time(row["target_time_utc"])
            received = _time(row["received_at"])
            checkpoint_at = _time(row["checkpoint_at"])
            lead = int(row["checkpoint_lead_seconds"])
            label_received = _time(row["label_received_at"])
            observation_after_target += int(received >= target)
            checkpoint_mismatches += int(
                abs((checkpoint_at - (target - timedelta(seconds=lead))).total_seconds())
                > 0.001
            )
            premature_labels += int(label_received <= received)
            invalid_model += int(
                row["model_probability"] is not None
                and _probability(row["model_probability"]) is None
            )
            invalid_market += int(
                row["market_yes_midpoint"] is not None
                and _probability(row["market_yes_midpoint"]) is None
            )
        reasons: list[str] = []
        for name, count in (
            ("observation_not_before_target", observation_after_target),
            ("checkpoint_timestamp_mismatch", checkpoint_mismatches),
            ("label_available_before_observation", premature_labels),
            ("invalid_model_probability", invalid_model),
            ("invalid_market_probability", invalid_market),
            ("replay_pnl_recompute_mismatch", replay_pnl_recompute_mismatch_count),
        ):
            if count:
                reasons.append(f"{name}:{count}")
        return BacktestIntegrity(
            observation_after_target_count=observation_after_target,
            checkpoint_timestamp_mismatch_count=checkpoint_mismatches,
            label_available_before_observation_count=premature_labels,
            invalid_model_probability_count=invalid_model,
            invalid_market_probability_count=invalid_market,
            replay_pnl_recompute_mismatch_count=replay_pnl_recompute_mismatch_count,
            passed=not reasons,
            reasons=tuple(reasons),
        )


def sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Hash a frozen snapshot without opening it in writable SQLite mode."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _rows_hash(
    probability_rows: Iterable[Row],
    replay_rows: Iterable[Row],
    *,
    prefix: Mapping[str, str],
) -> str:
    digest = hashlib.sha256()
    digest.update(_encoded(prefix))
    for kind, rows in (("probability", probability_rows), ("replay", replay_rows)):
        for row in rows:
            digest.update(kind.encode())
            digest.update(_encoded(dict(row)))
    return digest.hexdigest()


def _event_group(row: Mapping[str, Any] | Row) -> str:
    return f"{_asset(row)}|{_time(row['target_time_utc']).isoformat()}"


def _target_date(row: Mapping[str, Any] | Row) -> str:
    return _time(row["target_time_utc"]).date().isoformat()


def _asset(row: Mapping[str, Any] | Row) -> str:
    return str(row["asset"]).upper()


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShortChallengerBacktestError("naive_backtest_timestamp")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ShortChallengerBacktestError("backtest_clock_must_be_timezone_aware")
    return value.astimezone(UTC)


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _probability(value: object) -> float | None:
    parsed = _decimal(value)
    if parsed is None or parsed < _ZERO or parsed > _ONE:
        return None
    return float(parsed)


def _required_probability(value: object) -> float:
    probability = _probability(value)
    if probability is None:
        raise ShortChallengerBacktestError("paired_probability_became_invalid")
    return probability


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _hash(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _encoded(value: object) -> bytes:
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def _canonical(value: object) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value
