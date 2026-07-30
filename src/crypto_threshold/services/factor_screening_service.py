"""VectorBT-inspired pre-registered factor screening without auto-promotion."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from statistics import fmean
from typing import Any, cast

from crypto_threshold.domain.factor_research import (
    FactorComparator,
    FactorExperimentSpec,
    FactorObservation,
    FactorRule,
    FactorScreeningReport,
    FactorTradeSide,
    FactorTrialResult,
)

FACTOR_SCREENING_SOURCE_VERSION = "vectorbt-inspired-factor-screen-r3-v1"
_ZERO = Decimal("0")


class FactorScreeningError(ValueError):
    """A pre-registration or OOS-integrity invariant failed."""


class FactorScreeningService:
    """Retain every declared trial and compare it with both frozen baselines."""

    def seal_spec(
        self,
        *,
        experiment_id: str,
        spec_version: str,
        created_at: datetime,
        training_cutoff_at: datetime,
        minimum_oos_groups: int,
        minimum_dates: int,
        minimum_groups_per_asset: int,
        required_assets: tuple[str, ...],
        stake_usdc: Decimal,
        frozen_model_version: str,
        market_baseline_version: str,
        integrity_source_version: str,
        replay_source_version: str,
        rules: tuple[FactorRule, ...],
    ) -> FactorExperimentSpec:
        _aware(created_at, "created_at")
        _aware(training_cutoff_at, "training_cutoff_at")
        if _utc(created_at) <= _utc(training_cutoff_at):
            raise FactorScreeningError("spec_must_be_created_after_training_cutoff")
        if (
            minimum_oos_groups < 1
            or minimum_dates < 1
            or minimum_groups_per_asset < 1
            or stake_usdc <= 0
        ):
            raise FactorScreeningError("invalid_factor_experiment_minimums")
        normalized_assets = tuple(sorted({asset.upper() for asset in required_assets}))
        if not normalized_assets or len(normalized_assets) != len(required_assets):
            raise FactorScreeningError("required_assets_must_be_nonempty_and_unique")
        for value, field in (
            (experiment_id, "experiment_id"),
            (spec_version, "spec_version"),
            (frozen_model_version, "frozen_model_version"),
            (market_baseline_version, "market_baseline_version"),
            (integrity_source_version, "integrity_source_version"),
            (replay_source_version, "replay_source_version"),
        ):
            if not value.strip():
                raise FactorScreeningError(f"{field}_is_required")
        if not rules or len({rule.rule_id for rule in rules}) != len(rules):
            raise FactorScreeningError("factor_rules_must_be_nonempty_and_unique")
        for rule in rules:
            if not rule.rule_id.strip() or not rule.factor_name.strip():
                raise FactorScreeningError("factor_rule_identity_is_required")
        unsealed = FactorExperimentSpec(
            experiment_id=experiment_id,
            spec_version=spec_version,
            created_at=_utc(created_at),
            training_cutoff_at=_utc(training_cutoff_at),
            minimum_oos_groups=minimum_oos_groups,
            minimum_dates=minimum_dates,
            minimum_groups_per_asset=minimum_groups_per_asset,
            required_assets=normalized_assets,
            stake_usdc=stake_usdc,
            frozen_model_version=frozen_model_version,
            market_baseline_version=market_baseline_version,
            integrity_source_version=integrity_source_version,
            replay_source_version=replay_source_version,
            rules=rules,
            spec_hash="",
        )
        return replace(unsealed, spec_hash=_hash(_spec_payload(unsealed)))

    def verify_spec(self, spec: FactorExperimentSpec) -> None:
        if not spec.spec_hash or _hash(_spec_payload(spec)) != spec.spec_hash:
            raise FactorScreeningError("factor_experiment_spec_hash_mismatch")

    def screen(
        self,
        spec: FactorExperimentSpec,
        observations: tuple[FactorObservation, ...],
    ) -> FactorScreeningReport:
        self.verify_spec(spec)
        if not observations:
            raise FactorScreeningError("factor_screening_requires_observations")
        ordered = tuple(
            sorted(
                observations,
                key=lambda row: (_utc(row.target_time_utc), row.event_group_id),
            )
        )
        self._validate_observations(spec, ordered)
        groups = {row.event_group_id for row in ordered}
        dates = {_utc(row.target_time_utc).date().isoformat() for row in ordered}
        assets = tuple(sorted({row.asset.upper() for row in ordered}))
        per_asset = {
            asset: len(
                {row.event_group_id for row in ordered if row.asset.upper() == asset}
            )
            for asset in assets
        }
        coverage_reasons: list[str] = []
        if len(groups) < spec.minimum_oos_groups:
            coverage_reasons.append(
                f"insufficient_oos_groups:{len(groups)}/{spec.minimum_oos_groups}"
            )
        if len(dates) < spec.minimum_dates:
            coverage_reasons.append(
                f"insufficient_oos_dates:{len(dates)}/{spec.minimum_dates}"
            )
        for asset, count in per_asset.items():
            if count < spec.minimum_groups_per_asset:
                coverage_reasons.append(
                    f"insufficient_asset_groups:{asset}:{count}/"
                    f"{spec.minimum_groups_per_asset}"
                )
        for asset in spec.required_assets:
            if asset not in per_asset:
                coverage_reasons.append(f"missing_required_asset:{asset}")

        outcomes = [int(row.outcome_yes) for row in ordered]
        candidate_metrics = _metrics(
            [float(row.candidate_probability) for row in ordered],
            outcomes,
        )
        market_metrics = _metrics(
            [float(row.market_probability) for row in ordered],
            outcomes,
        )
        frozen_metrics = _metrics(
            [float(row.frozen_v4_probability) for row in ordered],
            outcomes,
        )
        trials = tuple(
            self._trial(
                spec,
                rule,
                ordered,
                coverage_reasons=tuple(coverage_reasons),
                candidate_metrics=candidate_metrics,
                market_metrics=market_metrics,
                frozen_metrics=frozen_metrics,
            )
            for rule in spec.rules
        )
        manifest_hash = _hash(
            {
                "source_version": FACTOR_SCREENING_SOURCE_VERSION,
                "spec": spec,
                "observations": ordered,
                "trials": trials,
            }
        )
        return FactorScreeningReport(
            experiment_id=spec.experiment_id,
            spec_hash=spec.spec_hash,
            observation_count=len(ordered),
            event_group_count=len(groups),
            date_count=len(dates),
            assets=assets,
            trials=trials,
            failed_trial_count=sum(not trial.screening_pass for trial in trials),
            manifest_hash=manifest_hash,
            promotion_allowed=False,
        )

    @staticmethod
    def _validate_observations(
        spec: FactorExperimentSpec,
        observations: tuple[FactorObservation, ...],
    ) -> None:
        groups: set[str] = set()
        observation_ids: set[str] = set()
        for row in observations:
            _aware(row.target_time_utc, "target_time_utc")
            _aware(row.decision_at, "decision_at")
            if row.event_group_id in groups:
                raise FactorScreeningError(
                    f"duplicate_event_group:{row.event_group_id}"
                )
            groups.add(row.event_group_id)
            if (
                not row.observation_id.strip()
                or row.observation_id in observation_ids
            ):
                raise FactorScreeningError(
                    f"duplicate_or_empty_observation_id:{row.observation_id}"
                )
            observation_ids.add(row.observation_id)
            if row.asset.upper() not in spec.required_assets:
                raise FactorScreeningError(
                    f"asset_not_preregistered:{row.asset.upper()}"
                )
            if _utc(row.target_time_utc) <= _utc(spec.training_cutoff_at):
                raise FactorScreeningError(
                    f"observation_not_oos:{row.event_group_id}"
                )
            if _utc(row.decision_at) >= _utc(row.target_time_utc):
                raise FactorScreeningError(
                    f"decision_not_before_target:{row.event_group_id}"
                )
            for value, field in (
                (row.candidate_probability, "candidate_probability"),
                (row.market_probability, "market_probability"),
                (row.frozen_v4_probability, "frozen_v4_probability"),
                (row.executable_yes_price, "executable_yes_price"),
                (row.executable_no_price, "executable_no_price"),
            ):
                if not _ZERO < value < Decimal("1"):
                    raise FactorScreeningError(
                        f"{field}_outside_open_unit_interval:{row.event_group_id}"
                    )
            if not _ZERO <= row.fill_ratio <= Decimal("1"):
                raise FactorScreeningError(
                    f"fill_ratio_out_of_range:{row.event_group_id}"
                )
            if row.fee_rate < 0:
                raise FactorScreeningError(
                    f"negative_fee_rate:{row.event_group_id}"
                )
            if (
                not row.integrity_manifest_hash
                or not row.replay_manifest_hash
            ):
                raise FactorScreeningError(
                    f"missing_integrity_or_replay_manifest:{row.event_group_id}"
                )

    @staticmethod
    def _trial(
        spec: FactorExperimentSpec,
        rule: FactorRule,
        observations: tuple[FactorObservation, ...],
        *,
        coverage_reasons: tuple[str, ...],
        candidate_metrics: dict[str, float],
        market_metrics: dict[str, float],
        frozen_metrics: dict[str, float],
    ) -> FactorTrialResult:
        triggered = [
            row
            for row in observations
            if _triggered(rule, row.factor_values.get(rule.factor_name))
        ]
        filled_stake = _ZERO
        attempted_stake = spec.stake_usdc * len(triggered)
        total_fees = _ZERO
        total_pnl = _ZERO
        total_fill_ratio = _ZERO
        for row in triggered:
            entry_price = (
                row.executable_yes_price
                if rule.trade_side is FactorTradeSide.YES
                else row.executable_no_price
            )
            filled = spec.stake_usdc * row.fill_ratio
            fee = filled * row.fee_rate
            shares = filled / entry_price if filled > 0 else _ZERO
            won = (
                row.outcome_yes
                if rule.trade_side is FactorTradeSide.YES
                else not row.outcome_yes
            )
            payout = shares if won else _ZERO
            filled_stake += filled
            total_fees += fee
            total_pnl += payout - filled - fee
            total_fill_ratio += row.fill_ratio
        net_ev = (
            total_pnl / attempted_stake if attempted_stake > 0 else None
        )
        average_fill = (
            total_fill_ratio / len(triggered) if triggered else None
        )
        reasons = list(coverage_reasons)
        if not triggered:
            reasons.append("no_triggered_oos_groups")
        if net_ev is None or net_ev <= 0:
            reasons.append("nonpositive_fee_adjusted_net_ev")
        if candidate_metrics["brier"] >= market_metrics["brier"]:
            reasons.append("candidate_does_not_beat_market_brier")
        if candidate_metrics["log_loss"] >= market_metrics["log_loss"]:
            reasons.append("candidate_does_not_beat_market_log_loss")
        if candidate_metrics["brier"] >= frozen_metrics["brier"]:
            reasons.append("candidate_does_not_beat_frozen_v4_brier")
        if average_fill is None or average_fill <= 0:
            reasons.append("no_conservative_replay_fill")
        reasons = list(dict.fromkeys(reasons))
        return FactorTrialResult(
            rule_id=rule.rule_id,
            factor_name=rule.factor_name,
            threshold=rule.threshold,
            comparator=rule.comparator,
            trade_side=rule.trade_side,
            evaluated_groups=len(observations),
            triggered_groups=len(triggered),
            filled_stake_usdc=filled_stake,
            total_fees_usdc=total_fees,
            total_pnl_usdc=total_pnl,
            net_ev_per_attempted_usdc=net_ev,
            average_fill_ratio=average_fill,
            candidate_metrics=dict(candidate_metrics),
            market_baseline_metrics=dict(market_metrics),
            frozen_v4_metrics=dict(frozen_metrics),
            screening_pass=not reasons,
            reasons=tuple(reasons),
        )


def _triggered(rule: FactorRule, value: Decimal | None) -> bool:
    if value is None:
        return False
    if rule.comparator is FactorComparator.GREATER_THAN:
        return value > rule.threshold
    return value < rule.threshold


def _metrics(probabilities: list[float], outcomes: list[int]) -> dict[str, float]:
    clipped = [min(max(value, 1e-12), 1 - 1e-12) for value in probabilities]
    brier = fmean(
        (probability - outcome) ** 2
        for probability, outcome in zip(clipped, outcomes, strict=True)
    )
    log_loss = -fmean(
        outcome * math.log(probability)
        + (1 - outcome) * math.log(1 - probability)
        for probability, outcome in zip(clipped, outcomes, strict=True)
    )
    ece = _ece(clipped, outcomes, bins=min(10, len(outcomes)))
    return {"brier": brier, "log_loss": log_loss, "ece": ece}


def _ece(probabilities: list[float], outcomes: list[int], *, bins: int) -> float:
    total = len(probabilities)
    result = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        selected = [
            item
            for item, probability in enumerate(probabilities)
            if lower <= probability < upper
            or (index == bins - 1 and probability == upper)
        ]
        if not selected:
            continue
        confidence = fmean(probabilities[item] for item in selected)
        accuracy = fmean(outcomes[item] for item in selected)
        result += len(selected) / total * abs(confidence - accuracy)
    return result


def _spec_payload(spec: FactorExperimentSpec) -> dict[str, object]:
    payload = asdict(spec)
    payload.pop("spec_hash", None)
    return payload


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FactorScreeningError(f"{field}_must_be_timezone_aware")


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
    if isinstance(value, Decimal):
        return str(value)
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
    return value
