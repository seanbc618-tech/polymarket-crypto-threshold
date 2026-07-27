"""Fixed-window out-of-sample calibration for sealed replay datasets."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from crypto_threshold.domain.research import CalibrationResult
from crypto_threshold.storage.repositories import Repository

CALIBRATION_SOURCE_VERSION = "fixed-holdout-calibration-v3"
CALIBRATION_MODEL_VERSION = "histogram-laplace-frozen-training-v3"
CALIBRATION_METHOD = "frozen_training_unique_label_histogram_laplace"


class CalibrationService:
    """Evaluate later labels against one immutable training replay."""

    def __init__(
        self,
        repository: Repository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        dataset: str,
        *,
        bins: int = 10,
        min_train_size: int = 30,
    ) -> CalibrationResult:
        if bins < 2:
            raise ValueError("calibration bins must be at least 2")
        if min_train_size < 1:
            raise ValueError("minimum train size must be positive")
        dataset_row = self.repository.get_replay_dataset(dataset)
        if dataset_row is None:
            raise ValueError(f"unknown replay dataset: {dataset}")
        rows = self.repository.replay_item_rows(str(dataset_row["dataset_id"]))
        samples = _latest_sample_per_label([_sample(row) for row in rows])
        started_at = _utc(self.clock())
        predictions: list[dict[str, float]] = []
        setup_rejection: str | None = None
        try:
            training_samples = _frozen_training_samples(
                self.repository,
                json.loads(str(dataset_row["config_json"])),
            )
        except ValueError as exc:
            training_samples = []
            setup_rejection = str(exc)
        training_label_ids = {
            str(sample["label_id"]) for sample in training_samples
        }
        oos_samples = [
            sample
            for sample in samples
            if str(sample["label_id"]) not in training_label_ids
        ]
        for test in oos_samples:
            training = [
                sample
                for sample in training_samples
                if sample["label_available_at"] < test["decision_at"]
            ]
            if (
                len(training_samples) < min_train_size
                or len(training) != len(training_samples)
            ):
                continue
            predictions.append(
                {
                    "raw": test["probability"],
                    "calibrated": _histogram_predict(
                        test["probability"], training, bins=bins
                    ),
                    "market": test["market_probability"],
                    "label": test["label"],
                }
            )

        status = "complete" if predictions else "insufficient_data"
        rejection = (
            None
            if predictions
            else setup_rejection or "no_valid_frozen_training_oos_window"
        )
        metrics = _metrics(predictions, bins=bins) if predictions else {}
        completed_at = _utc(self.clock())
        run_id = f"calibration:{uuid4()}"
        self.repository.save_calibration_run(
            run_id=run_id,
            dataset_id=str(dataset_row["dataset_id"]),
            status=status,
            method=CALIBRATION_METHOD,
            bins=bins,
            min_train_size=min_train_size,
            sample_count=len(samples),
            evaluated_count=len(predictions),
            metrics=metrics,
            rejection_reason=rejection,
            model_version=CALIBRATION_MODEL_VERSION,
            source_version=CALIBRATION_SOURCE_VERSION,
            started_at=started_at,
            completed_at=completed_at,
        )
        return CalibrationResult(
            run_id=run_id,
            dataset_id=str(dataset_row["dataset_id"]),
            status=status,
            sample_count=len(samples),
            evaluated_count=len(predictions),
            metrics=metrics,
            rejection_reason=rejection,
        )


def _frozen_training_samples(
    repository: Repository,
    combined_config: Any,
) -> list[dict[str, Any]]:
    if not isinstance(combined_config, dict):
        raise ValueError("invalid_combined_replay_config")
    reference = combined_config.get("training_reference")
    if not isinstance(reference, dict):
        raise ValueError("missing_frozen_training_reference")
    dataset_id = str(reference.get("dataset_id") or "")
    if not dataset_id:
        raise ValueError("missing_frozen_training_dataset_id")
    training_row = repository.get_replay_dataset(dataset_id)
    if training_row is None:
        raise ValueError("missing_frozen_training_dataset")
    if str(training_row["manifest_hash"]) != str(reference.get("manifest_hash") or ""):
        raise ValueError("frozen_training_manifest_hash_mismatch")
    training_config = json.loads(str(training_row["config_json"]))
    selection = (
        training_config.get("selection")
        if isinstance(training_config, dict)
        else None
    )
    if (
        not isinstance(selection, dict)
        or selection.get("mode") != "first_n_eligible_labels"
    ):
        raise ValueError("training_dataset_not_frozen")
    selected_labels = reference.get("selected_labels")
    if selected_labels != selection.get("selected_labels"):
        raise ValueError("frozen_training_label_manifest_mismatch")
    if reference.get("selected_unique_label_count") != selection.get(
        "selected_unique_label_count"
    ):
        raise ValueError("frozen_training_label_count_mismatch")
    if reference.get("training_cutoff") != selection.get("training_cutoff"):
        raise ValueError("frozen_training_cutoff_mismatch")
    if not isinstance(selected_labels, list):
        raise ValueError("invalid_frozen_training_label_manifest")
    selected_ids = {
        str(label.get("label_id") or "")
        for label in selected_labels
        if isinstance(label, dict)
    }
    if not selected_ids or "" in selected_ids:
        raise ValueError("invalid_frozen_training_label_ids")
    samples = _latest_sample_per_label(
        [
            _sample(row)
            for row in repository.replay_item_rows(str(training_row["dataset_id"]))
        ]
    )
    sample_ids = {str(sample["label_id"]) for sample in samples}
    if sample_ids != selected_ids:
        raise ValueError("frozen_training_sample_manifest_mismatch")
    return samples


def _sample(row: Any) -> dict[str, Any]:
    feature = json.loads(str(row["feature_payload"]))
    midpoint = feature.get("yes_midpoint")
    return {
        "label_id": str(row["label_id"]),
        "ordinal": int(row["ordinal"]),
        "decision_at": _time(row["decision_at"]),
        "label_available_at": _time(row["label_available_at"]),
        "probability": float(feature["estimated_probability"]),
        "market_probability": float(midpoint) if midpoint is not None else 0.5,
        "label": 1.0 if feature["outcome_yes"] else 0.0,
    }


def _latest_sample_per_label(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use one deterministic forecast per resolved contract outcome."""
    selected: dict[str, dict[str, Any]] = {}
    for sample in samples:
        label_id = str(sample["label_id"])
        if not label_id:
            raise ValueError("calibration sample is missing label_id")
        current = selected.get(label_id)
        candidate_key = (sample["decision_at"], sample["ordinal"])
        if current is None or candidate_key > (
            current["decision_at"],
            current["ordinal"],
        ):
            selected[label_id] = sample
    return sorted(
        selected.values(),
        key=lambda sample: (
            sample["decision_at"],
            sample["ordinal"],
            sample["label_id"],
        ),
    )


def _histogram_predict(probability: float, training: list[dict[str, Any]], *, bins: int) -> float:
    target_bin = _bin(probability, bins)
    members = [sample for sample in training if _bin(sample["probability"], bins) == target_bin]
    positives = sum(sample["label"] for sample in members)
    return (positives + 1.0) / (len(members) + 2.0)


def _metrics(predictions: list[dict[str, float]], *, bins: int) -> dict[str, Any]:
    labels = [item["label"] for item in predictions]
    return {
        "raw": _metric_set([item["raw"] for item in predictions], labels, bins=bins),
        "calibrated": _metric_set(
            [item["calibrated"] for item in predictions], labels, bins=bins
        ),
        "market_midpoint_baseline": _metric_set(
            [item["market"] for item in predictions], labels, bins=bins
        ),
    }


def _metric_set(probabilities: list[float], labels: list[float], *, bins: int) -> dict[str, float]:
    count = len(labels)
    clipped = [min(max(value, 1e-12), 1 - 1e-12) for value in probabilities]
    brier = sum((value - label) ** 2 for value, label in zip(probabilities, labels)) / count
    log_loss = -sum(
        label * math.log(value) + (1 - label) * math.log(1 - value)
        for value, label in zip(clipped, labels)
    ) / count
    ece = 0.0
    for bucket in range(bins):
        indexes = [
            index
            for index, value in enumerate(probabilities)
            if _bin(value, bins) == bucket
        ]
        if not indexes:
            continue
        confidence = sum(probabilities[index] for index in indexes) / len(indexes)
        accuracy = sum(labels[index] for index in indexes) / len(indexes)
        ece += len(indexes) / count * abs(confidence - accuracy)
    return {"brier": brier, "log_loss": log_loss, "ece": ece}


def _bin(probability: float, bins: int) -> int:
    return min(max(int(probability * bins), 0), bins - 1)


def _time(value: Any) -> datetime:
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
