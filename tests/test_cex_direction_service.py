"""Leakage, sealing, and chronological-holdout tests for the CEX model."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from crypto_threshold.domain.prices import Kline, KlineSeries
from crypto_threshold.services.cex_direction_service import (
    CEX_DIRECTION_FEATURE_NAMES,
    CEX_DIRECTION_SUPPORTED_ASSETS,
    CexDirectionArtifact,
    CexDirectionTrainingService,
    extract_cex_direction_features,
)

BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _series(asset: str, *, minutes: int = 460) -> KlineSeries:
    start = BASE - timedelta(minutes=40)
    price = Decimal("100")
    klines: list[Kline] = []
    for index in range(minutes):
        block = max(0, index - 40) // 5
        direction = Decimal("1") if block % 2 == 0 else Decimal("-1")
        opening = price
        closing = opening * (Decimal("1") + direction * Decimal("0.001"))
        high = max(opening, closing) * Decimal("1.0002")
        low = min(opening, closing) * Decimal("0.9998")
        open_time = start + timedelta(minutes=index)
        klines.append(
            Kline(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
                open=opening,
                high=high,
                low=low,
                close=closing,
                volume=Decimal("100") + Decimal(index % 17),
            )
        )
        price = closing
    return KlineSeries(
        asset=asset,
        quote="USDT",
        provider="binance",
        symbol=f"{asset}USDT",
        interval="1m",
        klines=tuple(klines),
        received_at=BASE + timedelta(days=1),
        source_version="binance-synthetic-v1",
        raw_payload=[],
    )


def _payload(klines: tuple[Kline, ...]) -> list[list[Any]]:
    return [
        [
            int(kline.open_time.timestamp() * 1000),
            str(kline.open),
            str(kline.high),
            str(kline.low),
            str(kline.close),
            str(kline.volume),
            int(kline.close_time.timestamp() * 1000),
        ]
        for kline in klines
    ]


def test_feature_extractor_ignores_every_future_candle() -> None:
    source = _series("BTC")
    target = BASE + timedelta(minutes=25)
    checkpoint = target - timedelta(minutes=1)
    window_start = target - timedelta(minutes=5)
    baseline = extract_cex_direction_features(
        source,
        asset="BTC",
        interval="5m",
        window_start_time_utc=window_start,
        checkpoint_at=checkpoint,
    )
    future = Kline(
        open_time=checkpoint,
        close_time=checkpoint + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=Decimal("1"),
        high=Decimal("1000000"),
        low=Decimal("0.1"),
        close=Decimal("999999"),
        volume=Decimal("999999"),
    )
    with_future = KlineSeries(
        **{
            **source.__dict__,
            "klines": source.klines + (future,),
        }
    )

    repeated = extract_cex_direction_features(
        with_future,
        asset="BTC",
        interval="5m",
        window_start_time_utc=window_start,
        checkpoint_at=checkpoint,
    )

    assert repeated.values == baseline.values
    assert repeated.latest_close_time == baseline.latest_close_time


def test_feature_extractor_accepts_last_closed_minute_at_t_minus_30() -> None:
    source = _series("BTC")
    target = BASE + timedelta(minutes=25)
    checkpoint = target - timedelta(seconds=30)

    features = extract_cex_direction_features(
        source,
        asset="BTC",
        interval="5m",
        window_start_time_utc=target - timedelta(minutes=5),
        checkpoint_at=checkpoint,
    )

    assert features.checkpoint_at == checkpoint
    assert features.latest_close_time == target - timedelta(minutes=1, milliseconds=1)
    assert features.latest_close_time < checkpoint


def test_artifact_hash_detects_any_coefficient_tamper(tmp_path: Path) -> None:
    provisional = CexDirectionArtifact(
        decision_lead_seconds=60,
        means=(0.0,) * len(CEX_DIRECTION_FEATURE_NAMES),
        scales=(1.0,) * len(CEX_DIRECTION_FEATURE_NAMES),
        weights=(0.0,) * len(CEX_DIRECTION_FEATURE_NAMES),
        intercept=0.0,
        probability_margin=0.05,
        training={"training_cutoff_time_utc": BASE.isoformat()},
        artifact_hash="pending",
    )
    payload = provisional.as_payload()
    path = tmp_path / "model.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert CexDirectionArtifact.load(path).artifact_hash == payload["artifact_hash"]

    payload["weights"][0] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        CexDirectionArtifact.load(path)


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def cex_direction_training_rows(
        self,
        *,
        assets: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        assert assets == CEX_DIRECTION_SUPPORTED_ASSETS
        return self.rows


class _Binance:
    def __init__(self, series: dict[str, KlineSeries]) -> None:
        self.series = series

    def get_klines(
        self,
        asset: str,
        interval: str,
        limit: int,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> KlineSeries:
        assert interval == "1m"
        source = self.series[asset]
        selected = tuple(
            kline
            for kline in source.klines
            if start_time is None or kline.open_time >= start_time
            if end_time is None or kline.open_time <= end_time
        )[:limit]
        return KlineSeries(
            **{
                **source.__dict__,
                "klines": selected,
                "raw_payload": _payload(selected),
            }
        )


def _training_rows(series: dict[str, KlineSeries]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for asset in CEX_DIRECTION_SUPPORTED_ASSETS:
        asset_series = series[asset]
        by_open = {kline.open_time: kline for kline in asset_series.klines}
        for block in range(80):
            target = BASE + timedelta(minutes=(block + 1) * 5)
            window_start = target - timedelta(minutes=5)
            checkpoint_candle = by_open[target - timedelta(minutes=2)]
            start_candle = by_open[window_start]
            outcome = checkpoint_candle.close >= start_candle.open
            rows.append(
                {
                    "label_id": f"label:{asset}:5m:{block}",
                    "market_id": f"market:{asset}:5m:{block}",
                    "asset": asset,
                    "candle_interval": "5m",
                    "window_start_time_utc": window_start.isoformat(),
                    "target_time_utc": target.isoformat(),
                    "outcome_yes": int(outcome),
                }
            )
        for group in range(26):
            target = BASE + timedelta(minutes=(group + 1) * 15)
            window_start = target - timedelta(minutes=15)
            checkpoint_candle = by_open[target - timedelta(minutes=2)]
            start_candle = by_open[window_start]
            outcome = checkpoint_candle.close >= start_candle.open
            rows.append(
                {
                    "label_id": f"label:{asset}:15m:{group}",
                    "market_id": f"market:{asset}:15m:{group}",
                    "asset": asset,
                    "candle_interval": "15m",
                    "window_start_time_utc": window_start.isoformat(),
                    "target_time_utc": target.isoformat(),
                    "outcome_yes": int(outcome),
                }
            )
    return rows


def test_training_fits_prefix_only_and_beats_constant_time_holdout(
    tmp_path: Path,
) -> None:
    series = {asset: _series(asset) for asset in CEX_DIRECTION_SUPPORTED_ASSETS}
    rows = _training_rows(series)
    output = tmp_path / "sealed.json"
    service = CexDirectionTrainingService(
        _Rows(rows),  # type: ignore[arg-type]
        _Binance(series),  # type: ignore[arg-type]
        clock=lambda: BASE + timedelta(days=2),
    )

    result = service.train(
        output,
        min_samples=500,
        epochs=300,
    )

    assert result.accepted
    assert result.sample_count == len(rows)
    assert result.holdout_count >= 100
    assert result.holdout_brier < result.baseline_brier
    assert result.holdout_log_loss < result.baseline_log_loss
    assert result.holdout_accuracy > 0.90
    assert output.is_file()
    artifact = CexDirectionArtifact.load(output)
    assert artifact.training["fit_scope"] == "chronological_prefix_only"
    assert artifact.training["holdout_first_target_time_utc"] > artifact.training[
        "training_cutoff_time_utc"
    ]
    assert math.isfinite(artifact.intercept)
