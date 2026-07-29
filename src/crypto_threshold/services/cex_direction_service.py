"""Leakage-safe CEX kline model for short Chainlink Up/Down contracts."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

from crypto_threshold.adapters.prices.binance import BinanceProvider
from crypto_threshold.domain.prices import Kline, KlineSeries
from crypto_threshold.storage.repositories import Repository

CEX_DIRECTION_MODEL_NAME = "cex_kline_chainlink_direction"
CEX_DIRECTION_MODEL_VERSION = "cex-kline-chainlink-direction-v1"
CEX_DIRECTION_FEATURE_VERSION = "cex-direction-features-v1"
CEX_DIRECTION_ARTIFACT_VERSION = "cex-direction-artifact-v1"
CEX_DIRECTION_SCHEMA_VERSION = 1
CEX_DIRECTION_SUPPORTED_ASSETS = ("BNB", "BTC", "DOGE", "ETH", "SOL", "XRP")

# BTC is the reference category. These normalized features are all known at the
# fixed pre-settlement checkpoint; no final Chainlink value enters the model.
CEX_DIRECTION_FEATURE_NAMES = (
    "window_return",
    "return_1m",
    "return_3m",
    "return_5m",
    "sma_spread_3_10",
    "vwap_deviation_15",
    "rsi14_centered",
    "realized_volatility_10",
    "range_mean_5",
    "body_ratio",
    "wick_skew",
    "volume_zscore_20",
    "interval_15m",
    "asset_bnb",
    "asset_doge",
    "asset_eth",
    "asset_sol",
    "asset_xrp",
)


@dataclass(frozen=True)
class CexDirectionFeatures:
    """Exact model features computed only from closed one-minute candles."""

    asset: str
    interval: str
    window_start_time_utc: datetime
    checkpoint_at: datetime
    latest_close_time: datetime
    latest_close: float
    values: tuple[float, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "interval": self.interval,
            "window_start_time_utc": _utc(
                self.window_start_time_utc
            ).isoformat(),
            "checkpoint_at": _utc(self.checkpoint_at).isoformat(),
            "latest_close_time": _utc(self.latest_close_time).isoformat(),
            "latest_close": self.latest_close,
            "feature_version": CEX_DIRECTION_FEATURE_VERSION,
            "features": dict(zip(CEX_DIRECTION_FEATURE_NAMES, self.values, strict=True)),
        }


@dataclass(frozen=True)
class CexDirectionArtifact:
    """Validated sealed logistic-model artifact."""

    decision_lead_seconds: int
    means: tuple[float, ...]
    scales: tuple[float, ...]
    weights: tuple[float, ...]
    intercept: float
    probability_margin: float
    training: dict[str, Any]
    artifact_hash: str
    model_version: str = CEX_DIRECTION_MODEL_VERSION

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CexDirectionArtifact:
        if payload.get("schema_version") != CEX_DIRECTION_SCHEMA_VERSION:
            raise ValueError("unsupported CEX direction artifact schema")
        if payload.get("model_name") != CEX_DIRECTION_MODEL_NAME:
            raise ValueError("unexpected CEX direction model name")
        if payload.get("model_version") != CEX_DIRECTION_MODEL_VERSION:
            raise ValueError("unexpected CEX direction model version")
        if payload.get("feature_version") != CEX_DIRECTION_FEATURE_VERSION:
            raise ValueError("unexpected CEX direction feature version")
        if tuple(payload.get("feature_names") or ()) != CEX_DIRECTION_FEATURE_NAMES:
            raise ValueError("CEX direction feature manifest mismatch")

        expected_hash = str(payload.get("artifact_hash") or "")
        unsigned = {key: value for key, value in payload.items() if key != "artifact_hash"}
        if not expected_hash or _hash(unsigned) != expected_hash:
            raise ValueError("CEX direction artifact hash mismatch")

        means = _finite_vector(payload.get("means"), "means")
        scales = _finite_vector(payload.get("scales"), "scales")
        weights = _finite_vector(payload.get("weights"), "weights")
        expected_size = len(CEX_DIRECTION_FEATURE_NAMES)
        if not (len(means) == len(scales) == len(weights) == expected_size):
            raise ValueError("CEX direction artifact vector length mismatch")
        if any(scale <= 0 for scale in scales):
            raise ValueError("CEX direction artifact has a non-positive scale")

        intercept = _finite_float(payload.get("intercept"), "intercept")
        margin = _finite_float(
            payload.get("probability_margin"), "probability_margin"
        )
        if not 0 <= margin <= 0.25:
            raise ValueError("CEX direction probability margin is outside [0, 0.25]")
        lead_seconds = int(payload.get("decision_lead_seconds") or 0)
        if lead_seconds < 30 or lead_seconds > 300:
            raise ValueError("CEX direction decision lead is outside [30, 300]")
        training = payload.get("training")
        if not isinstance(training, dict):
            raise ValueError("CEX direction artifact is missing training metadata")

        return cls(
            decision_lead_seconds=lead_seconds,
            means=means,
            scales=scales,
            weights=weights,
            intercept=intercept,
            probability_margin=margin,
            training=dict(training),
            artifact_hash=expected_hash,
        )

    @classmethod
    def load(cls, path: str | Path) -> CexDirectionArtifact:
        artifact_path = Path(path).expanduser()
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid CEX direction artifact: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise ValueError("CEX direction artifact must be a JSON object")
        return cls.from_payload(payload)

    @property
    def runtime_model_version(self) -> str:
        return f"{self.model_version}+{self.artifact_hash[:12]}"

    def predict(self, features: CexDirectionFeatures) -> float:
        if len(features.values) != len(self.weights):
            raise ValueError("CEX direction feature vector length mismatch")
        standardized = tuple(
            (value - mean) / scale
            for value, mean, scale in zip(
                features.values,
                self.means,
                self.scales,
                strict=True,
            )
        )
        score = self.intercept + sum(
            weight * value
            for weight, value in zip(self.weights, standardized, strict=True)
        )
        return _sigmoid(score)

    def as_payload(self) -> dict[str, Any]:
        unsigned: dict[str, Any] = {
            "schema_version": CEX_DIRECTION_SCHEMA_VERSION,
            "model_name": CEX_DIRECTION_MODEL_NAME,
            "model_version": self.model_version,
            "feature_version": CEX_DIRECTION_FEATURE_VERSION,
            "feature_names": list(CEX_DIRECTION_FEATURE_NAMES),
            "decision_lead_seconds": self.decision_lead_seconds,
            "means": list(self.means),
            "scales": list(self.scales),
            "weights": list(self.weights),
            "intercept": self.intercept,
            "probability_margin": self.probability_margin,
            "training": self.training,
        }
        return {**unsigned, "artifact_hash": _hash(unsigned)}


@dataclass(frozen=True)
class CexDirectionTrainingResult:
    """Human-readable evidence returned after a sealed training run."""

    artifact: CexDirectionArtifact
    output_path: Path
    sample_count: int
    training_count: int
    holdout_count: int
    holdout_brier: float
    holdout_log_loss: float
    holdout_accuracy: float
    baseline_brier: float
    baseline_log_loss: float
    baseline_accuracy: float
    accepted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _TrainingSample:
    label_id: str
    market_id: str
    asset: str
    interval: str
    target_time_utc: datetime
    outcome_up: int
    features: CexDirectionFeatures


def extract_cex_direction_features(
    series: KlineSeries,
    *,
    asset: str,
    interval: str,
    window_start_time_utc: datetime,
    checkpoint_at: datetime,
) -> CexDirectionFeatures:
    """Build a deterministic feature vector from candles closed by checkpoint."""

    normalized_asset = asset.upper()
    if normalized_asset not in CEX_DIRECTION_SUPPORTED_ASSETS:
        raise ValueError(f"unsupported CEX direction asset: {normalized_asset}")
    if interval not in {"5m", "15m"}:
        raise ValueError(f"unsupported CEX direction interval: {interval}")
    checkpoint = _utc(checkpoint_at)
    window_start = _utc(window_start_time_utc)
    if window_start >= checkpoint:
        raise ValueError("CEX direction checkpoint must follow the window start")

    closed_by_open: dict[datetime, Kline] = {}
    for kline in series.klines:
        open_time = _utc(kline.open_time)
        if _utc(kline.close_time) <= checkpoint:
            closed_by_open[open_time] = kline
    closed = [closed_by_open[key] for key in sorted(closed_by_open)]
    if len(closed) < 21:
        raise ValueError("insufficient closed CEX kline history")

    recent = closed[-21:]
    for previous, current in zip(recent, recent[1:], strict=False):
        seconds = (_utc(current.open_time) - _utc(previous.open_time)).total_seconds()
        if seconds != 60:
            raise ValueError("CEX kline history contains a gap")
    latest = recent[-1]
    if abs((checkpoint - _utc(latest.close_time)).total_seconds()) > 1.1:
        raise ValueError("latest closed CEX candle does not end at checkpoint")
    window_candle = closed_by_open.get(window_start)
    if window_candle is None:
        raise ValueError("missing CEX candle at Chainlink window start")

    closes = [float(kline.close) for kline in recent]
    volumes = [float(kline.volume) for kline in recent]
    if any(value <= 0 or not math.isfinite(value) for value in closes):
        raise ValueError("CEX kline history has an invalid close")
    if any(value < 0 or not math.isfinite(value) for value in volumes):
        raise ValueError("CEX kline history has an invalid volume")
    window_open = float(window_candle.open)
    if window_open <= 0 or not math.isfinite(window_open):
        raise ValueError("CEX window-open candle is invalid")

    latest_close = closes[-1]
    window_return = _clamp(math.log(latest_close / window_open), -0.10, 0.10)
    return_1m = _log_return(closes, 1)
    return_3m = _log_return(closes, 3)
    return_5m = _log_return(closes, 5)
    sma_spread = _clamp(
        (fmean(closes[-3:]) - fmean(closes[-10:])) / latest_close,
        -0.10,
        0.10,
    )

    vwap_candles = recent[-15:]
    total_volume = sum(float(kline.volume) for kline in vwap_candles)
    if total_volume > 0:
        vwap = sum(
            (
                (
                    float(kline.high)
                    + float(kline.low)
                    + float(kline.close)
                )
                / 3
            )
            * float(kline.volume)
            for kline in vwap_candles
        ) / total_volume
    else:
        vwap = fmean(float(kline.close) for kline in vwap_candles)
    vwap_deviation = _clamp((latest_close - vwap) / latest_close, -0.10, 0.10)

    rsi_changes = [
        closes[index] - closes[index - 1]
        for index in range(len(closes) - 14, len(closes))
    ]
    average_gain = fmean(max(change, 0.0) for change in rsi_changes)
    average_loss = fmean(max(-change, 0.0) for change in rsi_changes)
    if average_loss == 0:
        rsi = 100.0 if average_gain > 0 else 50.0
    else:
        relative_strength = average_gain / average_loss
        rsi = 100.0 - 100.0 / (1.0 + relative_strength)
    rsi_centered = _clamp((rsi - 50.0) / 50.0, -1.0, 1.0)

    log_returns = [
        math.log(closes[index] / closes[index - 1])
        for index in range(len(closes) - 10, len(closes))
    ]
    realized_volatility = _clamp(pstdev(log_returns), 0.0, 0.10)
    range_mean = _clamp(
        fmean(
            (float(kline.high) - float(kline.low)) / float(kline.close)
            for kline in recent[-5:]
        ),
        0.0,
        0.10,
    )

    latest_open = float(latest.open)
    latest_high = float(latest.high)
    latest_low = float(latest.low)
    latest_range = latest_high - latest_low
    if latest_range > 0:
        body_ratio = _clamp(
            (latest_close - latest_open) / latest_range, -1.0, 1.0
        )
        lower_wick = min(latest_open, latest_close) - latest_low
        upper_wick = latest_high - max(latest_open, latest_close)
        wick_skew = _clamp((lower_wick - upper_wick) / latest_range, -1.0, 1.0)
    else:
        body_ratio = 0.0
        wick_skew = 0.0

    volume_mean = fmean(volumes[-20:])
    volume_std = pstdev(volumes[-20:])
    volume_zscore = (
        _clamp((volumes[-1] - volume_mean) / volume_std, -5.0, 5.0)
        if volume_std > 0
        else 0.0
    )

    asset_flags = {
        "BNB": (1.0, 0.0, 0.0, 0.0, 0.0),
        "BTC": (0.0, 0.0, 0.0, 0.0, 0.0),
        "DOGE": (0.0, 1.0, 0.0, 0.0, 0.0),
        "ETH": (0.0, 0.0, 1.0, 0.0, 0.0),
        "SOL": (0.0, 0.0, 0.0, 1.0, 0.0),
        "XRP": (0.0, 0.0, 0.0, 0.0, 1.0),
    }
    values = (
        window_return,
        return_1m,
        return_3m,
        return_5m,
        sma_spread,
        vwap_deviation,
        rsi_centered,
        realized_volatility,
        range_mean,
        body_ratio,
        wick_skew,
        volume_zscore,
        1.0 if interval == "15m" else 0.0,
        *asset_flags[normalized_asset],
    )
    if len(values) != len(CEX_DIRECTION_FEATURE_NAMES) or not all(
        math.isfinite(value) for value in values
    ):
        raise ValueError("CEX direction feature vector is invalid")
    return CexDirectionFeatures(
        asset=normalized_asset,
        interval=interval,
        window_start_time_utc=window_start,
        checkpoint_at=checkpoint,
        latest_close_time=_utc(latest.close_time),
        latest_close=latest_close,
        values=tuple(values),
    )


class CexDirectionTrainingService:
    """Train once on a chronological prefix and report a sealed holdout."""

    def __init__(
        self,
        repository: Repository,
        binance: BinanceProvider,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.binance = binance
        self.clock = clock or (lambda: datetime.now(UTC))

    def train(
        self,
        output_path: str | Path,
        *,
        decision_lead_seconds: int = 60,
        holdout_fraction: float = 0.25,
        min_samples: int = 500,
        epochs: int = 600,
        learning_rate: float = 0.08,
        l2_penalty: float = 0.02,
    ) -> CexDirectionTrainingResult:
        if not 30 <= decision_lead_seconds <= 300:
            raise ValueError("decision lead seconds must be within [30, 300]")
        if not 0.10 <= holdout_fraction <= 0.50:
            raise ValueError("holdout fraction must be within [0.10, 0.50]")
        if min_samples < 100:
            raise ValueError("minimum samples must be at least 100")
        if epochs < 1 or learning_rate <= 0 or l2_penalty < 0:
            raise ValueError("invalid logistic training parameters")

        rows = self.repository.cex_direction_training_rows(
            assets=CEX_DIRECTION_SUPPORTED_ASSETS
        )
        labels = [_training_label(row) for row in rows]
        if len(labels) < min_samples:
            raise ValueError(
                f"insufficient authoritative Chainlink labels: {len(labels)}/{min_samples}"
            )
        klines_by_asset = self._historical_klines(
            labels,
            decision_lead_seconds=decision_lead_seconds,
        )
        samples: list[_TrainingSample] = []
        rejected: list[str] = []
        for label in labels:
            checkpoint = label["target_time_utc"] - timedelta(
                seconds=decision_lead_seconds
            )
            try:
                features = extract_cex_direction_features(
                    klines_by_asset[str(label["asset"])],
                    asset=str(label["asset"]),
                    interval=str(label["interval"]),
                    window_start_time_utc=label["window_start_time_utc"],
                    checkpoint_at=checkpoint,
                )
            except ValueError as exc:
                rejected.append(f"{label['label_id']}:{exc}")
                continue
            samples.append(
                _TrainingSample(
                    label_id=str(label["label_id"]),
                    market_id=str(label["market_id"]),
                    asset=str(label["asset"]),
                    interval=str(label["interval"]),
                    target_time_utc=label["target_time_utc"],
                    outcome_up=int(label["outcome_up"]),
                    features=features,
                )
            )
        if len(samples) < min_samples:
            raise ValueError(
                "insufficient leakage-safe CEX feature rows: "
                f"{len(samples)}/{min_samples}; rejected={len(rejected)}"
            )

        unique_targets = sorted({sample.target_time_utc for sample in samples})
        split_index = int(len(unique_targets) * (1.0 - holdout_fraction))
        split_index = min(max(split_index, 1), len(unique_targets) - 1)
        training_cutoff = unique_targets[split_index - 1]
        training_samples = [
            sample for sample in samples if sample.target_time_utc <= training_cutoff
        ]
        holdout_samples = [
            sample for sample in samples if sample.target_time_utc > training_cutoff
        ]
        if len(training_samples) < 300 or len(holdout_samples) < 100:
            raise ValueError(
                "chronological split is too small: "
                f"train={len(training_samples)} holdout={len(holdout_samples)}"
            )

        means, scales = _fit_standardizer(training_samples)
        weights, intercept = _fit_logistic(
            training_samples,
            means=means,
            scales=scales,
            epochs=epochs,
            learning_rate=learning_rate,
            l2_penalty=l2_penalty,
        )
        holdout_probabilities = [
            _predict(
                sample.features.values,
                means=means,
                scales=scales,
                weights=weights,
                intercept=intercept,
            )
            for sample in holdout_samples
        ]
        outcomes = [sample.outcome_up for sample in holdout_samples]
        holdout_metrics = _metrics(holdout_probabilities, outcomes)
        train_up_rate = fmean(sample.outcome_up for sample in training_samples)
        baseline_metrics = _metrics(
            [train_up_rate] * len(holdout_samples),
            outcomes,
        )
        holdout_ece = _ece(holdout_probabilities, outcomes, bins=10)
        # Fixed before inspecting the holdout. The holdout remains evaluation
        # evidence and never tunes coefficients, calibration, or this margin.
        probability_margin = 0.05

        reasons: list[str] = []
        if holdout_metrics["brier"] >= baseline_metrics["brier"]:
            reasons.append("holdout_brier_not_better_than_constant_baseline")
        if holdout_metrics["log_loss"] >= baseline_metrics["log_loss"]:
            reasons.append("holdout_log_loss_not_better_than_constant_baseline")
        if holdout_metrics["accuracy"] <= 0.50:
            reasons.append("holdout_accuracy_not_above_chance")
        covered_assets = {sample.asset for sample in training_samples}
        if len(covered_assets) < 4:
            reasons.append("training_asset_coverage_below_four")
        accepted = not reasons

        dataset_payload = [
            {
                "label_id": sample.label_id,
                "market_id": sample.market_id,
                "asset": sample.asset,
                "interval": sample.interval,
                "target_time_utc": sample.target_time_utc.isoformat(),
                "outcome_up": sample.outcome_up,
                "features": sample.features.as_payload(),
            }
            for sample in samples
        ]
        training_metadata: dict[str, Any] = {
            "trained_at": _utc(self.clock()).isoformat(),
            "label_source": "authoritative Chainlink settlement_labels",
            "kline_source": "Binance public spot 1m closed klines",
            "fit_scope": "chronological_prefix_only",
            "decision_checkpoint": f"T-{decision_lead_seconds}s",
            "sample_count": len(samples),
            "training_count": len(training_samples),
            "holdout_count": len(holdout_samples),
            "rejected_feature_rows": len(rejected),
            "first_target_time_utc": min(
                sample.target_time_utc for sample in samples
            ).isoformat(),
            "last_target_time_utc": max(
                sample.target_time_utc for sample in samples
            ).isoformat(),
            "training_cutoff_time_utc": training_cutoff.isoformat(),
            "holdout_first_target_time_utc": min(
                sample.target_time_utc for sample in holdout_samples
            ).isoformat(),
            "assets": sorted(covered_assets),
            "asset_counts": _counts(training_samples, "asset"),
            "interval_counts": _counts(training_samples, "interval"),
            "training_up_rate": train_up_rate,
            "holdout_up_rate": fmean(outcomes),
            "holdout_brier": holdout_metrics["brier"],
            "holdout_log_loss": holdout_metrics["log_loss"],
            "holdout_accuracy": holdout_metrics["accuracy"],
            "holdout_ece": holdout_ece,
            "runtime_probability_margin": probability_margin,
            "baseline_brier": baseline_metrics["brier"],
            "baseline_log_loss": baseline_metrics["log_loss"],
            "baseline_accuracy": baseline_metrics["accuracy"],
            "dataset_hash": _hash(dataset_payload),
            "optimizer": {
                "algorithm": "batch_logistic_regression",
                "epochs": epochs,
                "learning_rate": learning_rate,
                "l2_penalty": l2_penalty,
            },
            "acceptance_reasons": reasons,
        }
        provisional = CexDirectionArtifact(
            decision_lead_seconds=decision_lead_seconds,
            means=means,
            scales=scales,
            weights=weights,
            intercept=intercept,
            probability_margin=probability_margin,
            training=training_metadata,
            artifact_hash="pending",
        )
        artifact = CexDirectionArtifact.from_payload(provisional.as_payload())
        path = Path(output_path).expanduser()
        if not accepted:
            raise ValueError(
                "CEX direction holdout gate failed: " + ",".join(reasons)
            )
        _write_artifact(path, artifact.as_payload())
        return CexDirectionTrainingResult(
            artifact=artifact,
            output_path=path.resolve(),
            sample_count=len(samples),
            training_count=len(training_samples),
            holdout_count=len(holdout_samples),
            holdout_brier=holdout_metrics["brier"],
            holdout_log_loss=holdout_metrics["log_loss"],
            holdout_accuracy=holdout_metrics["accuracy"],
            baseline_brier=baseline_metrics["brier"],
            baseline_log_loss=baseline_metrics["log_loss"],
            baseline_accuracy=baseline_metrics["accuracy"],
            accepted=accepted,
            reasons=tuple(reasons),
        )

    def _historical_klines(
        self,
        labels: list[dict[str, Any]],
        *,
        decision_lead_seconds: int,
    ) -> dict[str, KlineSeries]:
        result: dict[str, KlineSeries] = {}
        for asset in sorted({str(label["asset"]) for label in labels}):
            asset_labels = [label for label in labels if label["asset"] == asset]
            start = min(
                label["window_start_time_utc"] for label in asset_labels
            ) - timedelta(minutes=30)
            end = max(label["target_time_utc"] for label in asset_labels) - timedelta(
                seconds=decision_lead_seconds
            )
            result[asset] = self._fetch_range(asset, start=start, end=end)
        return result

    def _fetch_range(
        self,
        asset: str,
        *,
        start: datetime,
        end: datetime,
    ) -> KlineSeries:
        cursor = _floor_minute(start)
        end = _utc(end)
        by_open: dict[datetime, Kline] = {}
        latest_series: KlineSeries | None = None
        while cursor <= end:
            series = self.binance.get_klines(
                asset,
                interval="1m",
                limit=1000,
                start_time=cursor,
                end_time=end,
            )
            latest_series = series
            if not series.klines:
                break
            for kline in series.klines:
                if _utc(kline.close_time) <= end:
                    by_open[_utc(kline.open_time)] = kline
            next_cursor = max(_utc(kline.open_time) for kline in series.klines) + timedelta(
                minutes=1
            )
            if next_cursor <= cursor:
                raise ValueError(f"Binance kline pagination stalled for {asset}")
            cursor = next_cursor
            if len(series.klines) < 1000:
                break
        if latest_series is None or not by_open:
            raise ValueError(f"no Binance historical klines for {asset}")
        return replace(
            latest_series,
            klines=tuple(by_open[key] for key in sorted(by_open)),
            raw_payload={
                "range_start": _utc(start).isoformat(),
                "range_end": end.isoformat(),
                "kline_count": len(by_open),
            },
        )


def _training_label(row: Any) -> dict[str, Any]:
    return {
        "label_id": str(row["label_id"]),
        "market_id": str(row["market_id"]),
        "asset": str(row["asset"]).upper(),
        "interval": str(row["candle_interval"]),
        "window_start_time_utc": _time(row["window_start_time_utc"]),
        "target_time_utc": _time(row["target_time_utc"]),
        "outcome_up": int(bool(row["outcome_yes"])),
    }


def _fit_standardizer(
    samples: list[_TrainingSample],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    columns = list(zip(*(sample.features.values for sample in samples), strict=True))
    means = tuple(fmean(column) for column in columns)
    scales = tuple(max(pstdev(column), 1e-9) for column in columns)
    return means, scales


def _fit_logistic(
    samples: list[_TrainingSample],
    *,
    means: tuple[float, ...],
    scales: tuple[float, ...],
    epochs: int,
    learning_rate: float,
    l2_penalty: float,
) -> tuple[tuple[float, ...], float]:
    vectors = [
        tuple(
            (value - mean) / scale
            for value, mean, scale in zip(
                sample.features.values,
                means,
                scales,
                strict=True,
            )
        )
        for sample in samples
    ]
    outcomes = [sample.outcome_up for sample in samples]
    up_rate = _clamp(fmean(outcomes), 1e-6, 1.0 - 1e-6)
    intercept = math.log(up_rate / (1.0 - up_rate))
    weights = [0.0] * len(CEX_DIRECTION_FEATURE_NAMES)
    count = float(len(samples))

    for epoch in range(epochs):
        intercept_gradient = 0.0
        gradients = [0.0] * len(weights)
        for vector, outcome in zip(vectors, outcomes, strict=True):
            score = intercept + sum(
                weight * value
                for weight, value in zip(weights, vector, strict=True)
            )
            error = _sigmoid(score) - outcome
            intercept_gradient += error
            for index, value in enumerate(vector):
                gradients[index] += error * value
        step = learning_rate / math.sqrt(1.0 + epoch / 100.0)
        intercept -= step * intercept_gradient / count
        for index in range(len(weights)):
            gradient = gradients[index] / count + l2_penalty * weights[index]
            weights[index] -= step * gradient

    if not math.isfinite(intercept) or not all(math.isfinite(value) for value in weights):
        raise ValueError("logistic optimizer produced non-finite coefficients")
    return tuple(weights), intercept


def _predict(
    values: tuple[float, ...],
    *,
    means: tuple[float, ...],
    scales: tuple[float, ...],
    weights: tuple[float, ...],
    intercept: float,
) -> float:
    score = intercept + sum(
        weight * ((value - mean) / scale)
        for value, mean, scale, weight in zip(
            values,
            means,
            scales,
            weights,
            strict=True,
        )
    )
    return _sigmoid(score)


def _metrics(probabilities: list[float], outcomes: list[int]) -> dict[str, float]:
    if not probabilities or len(probabilities) != len(outcomes):
        raise ValueError("metric inputs must be non-empty and aligned")
    clipped = [_clamp(probability, 1e-12, 1.0 - 1e-12) for probability in probabilities]
    return {
        "brier": fmean(
            (probability - outcome) ** 2
            for probability, outcome in zip(clipped, outcomes, strict=True)
        ),
        "log_loss": -fmean(
            outcome * math.log(probability)
            + (1 - outcome) * math.log(1.0 - probability)
            for probability, outcome in zip(clipped, outcomes, strict=True)
        ),
        "accuracy": fmean(
            int((probability >= 0.5) == bool(outcome))
            for probability, outcome in zip(clipped, outcomes, strict=True)
        ),
    }


def _ece(probabilities: list[float], outcomes: list[int], *, bins: int) -> float:
    total = len(probabilities)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        bucket = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes, strict=True)
            if lower <= probability < upper or (index == bins - 1 and probability == 1)
        ]
        if not bucket:
            continue
        confidence = fmean(item[0] for item in bucket)
        frequency = fmean(item[1] for item in bucket)
        error += len(bucket) / total * abs(confidence - frequency)
    return error


def _counts(samples: list[_TrainingSample], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sample in samples:
        key = str(getattr(sample, field))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(resolved)
    finally:
        temporary.unlink(missing_ok=True)


def _finite_vector(value: Any, name: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ValueError(f"CEX direction artifact {name} must be a list")
    return tuple(_finite_float(item, name) for item in value)


def _finite_float(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"CEX direction artifact {name} is not numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"CEX direction artifact {name} is not finite")
    return parsed


def _log_return(closes: list[float], periods: int) -> float:
    return _clamp(math.log(closes[-1] / closes[-periods - 1]), -0.10, 0.10)


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(max(value, -60.0))
    return exponent / (1.0 + exponent)


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _floor_minute(value: datetime) -> datetime:
    return _utc(value).replace(second=0, microsecond=0)


def _time(value: Any) -> datetime:
    if not value:
        raise ValueError("required CEX direction timestamp is missing")
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
