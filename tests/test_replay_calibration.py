"""Immutable replay and leakage-safe walk-forward calibration tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from crypto_threshold.config import Settings
from crypto_threshold.services.calibration_service import CalibrationService
from crypto_threshold.services.market_workflow_service import MarketWorkflowService
from crypto_threshold.services.replay_service import ReplayService
from crypto_threshold.services.settlement_service import SettlementService
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository
from tests.conftest import (
    NOW,
    TARGET,
    FakeBinanceProvider,
    FakeCoinbaseProvider,
    FakePolymarketClient,
    make_market_payload,
)
from tests.test_settlement_service import SettlementBinance


def test_replay_seals_exact_inputs_and_detects_tampering(tmp_path: Path) -> None:
    database = Database(tmp_path / "replay.db")
    database.initialize()
    repository = Repository(database)
    payload = make_market_payload()
    workflow = MarketWorkflowService(
        client=FakePolymarketClient(payload),
        repository=repository,
        binance=FakeBinanceProvider(),
        coinbase=FakeCoinbaseProvider(),
        settings=Settings(DATABASE_PATH=str(database.path), _env_file=None),
        clock=lambda: NOW,
    )
    signal = workflow.analyze("market-1")
    SettlementService(
        repository=repository,
        binance=SettlementBinance(signal.threshold + 1),  # type: ignore[arg-type,operator]
        clock=lambda: TARGET + timedelta(minutes=2),
    ).settle_market("market-1")

    service = ReplayService(repository, clock=lambda: TARGET + timedelta(minutes=3))
    built = service.build("acceptance-v1")
    assert built.item_count == 1
    assert built.rejection_reasons == ()
    verified = service.verify(built.dataset_id)
    assert verified.ok
    assert verified.verified_count == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.transaction() as connection:
            connection.execute(
                "UPDATE replay_datasets SET manifest_hash = 'changed' WHERE dataset_id = ?",
                (built.dataset_id,),
            )

    with database.transaction() as connection:
        payload_id = connection.execute(
            """
            SELECT payload_id FROM analysis_signal_inputs
            WHERE signal_id = ? ORDER BY payload_id LIMIT 1
            """,
            (signal.signal_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE external_payloads SET raw_payload = '{\"tampered\":true}' WHERE id = ?",
            (payload_id,),
        )
    tampered = service.verify(built.dataset_id)
    assert not tampered.ok
    assert any("input_manifest_hash_mismatch" in reason for reason in tampered.reasons)


def test_calibration_is_insufficient_when_prior_labels_were_not_yet_visible(
    tmp_path: Path,
) -> None:
    repository, dataset_id = _calibration_dataset(tmp_path, labels_visible=False)
    result = CalibrationService(repository).run(dataset_id, bins=5, min_train_size=2)
    assert result.status == "insufficient_data"
    assert result.evaluated_count == 0


def test_empty_replay_manifest_is_not_an_acceptance_pass(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()
    service = ReplayService(Repository(database))
    built = service.build("empty")
    verified = service.verify(built.dataset_id)
    assert built.item_count == 0
    assert not verified.ok
    assert verified.reasons == ("empty_dataset",)


def test_calibration_reports_raw_calibrated_and_market_baseline(tmp_path: Path) -> None:
    repository, dataset_id = _calibration_dataset(tmp_path, labels_visible=True)
    result = CalibrationService(repository).run(dataset_id, bins=5, min_train_size=2)
    assert result.status == "complete"
    assert result.sample_count == 4
    assert result.evaluated_count == 2
    assert set(result.metrics) == {"raw", "calibrated", "market_midpoint_baseline"}
    assert set(result.metrics["raw"]) == {"brier", "log_loss", "ece"}


def test_calibration_counts_repeated_snapshots_as_one_settlement_label(
    tmp_path: Path,
) -> None:
    repository, dataset_id = _calibration_dataset(
        tmp_path,
        labels_visible=True,
        snapshots_per_label=3,
    )
    result = CalibrationService(repository).run(dataset_id, bins=5, min_train_size=2)
    assert result.status == "complete"
    assert result.sample_count == 4
    assert result.evaluated_count == 2


def _calibration_dataset(
    tmp_path: Path,
    *,
    labels_visible: bool,
    snapshots_per_label: int = 1,
) -> tuple[Repository, str]:
    database = Database(tmp_path / f"calibration-{labels_visible}.db")
    database.initialize()
    repository = Repository(database)
    dataset_id = f"dataset-{labels_visible}"
    items = []
    ordinal = 0
    with database.transaction() as connection:
        for index in range(4):
            market_id = f"market-{index}"
            label_id = f"label-{index}"
            first_decision = NOW + timedelta(days=index)
            label_available = (
                first_decision + timedelta(hours=1)
                if labels_visible
                else NOW + timedelta(days=10)
            )
            connection.execute(
                "INSERT INTO markets (market_id, question) VALUES (?, ?)",
                (market_id, market_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO external_payloads (
                    market_id, source, payload_kind, received_at, source_version, raw_payload
                ) VALUES (?, 'binance', 'settlement', ?, 'test', '{}')
                """,
                (market_id, label_available.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO settlement_labels (
                    label_id, market_id, target_time_utc, provider, pair,
                    candle_interval, price_field, exact_operator, strike,
                    observed_value, outcome_yes, payload_id, observed_at,
                    received_at, source_version
                ) VALUES (?, ?, ?, 'binance', 'BTC/USDT', '1m', 'Close', '>',
                          '100', '101', ?, ?, ?, ?, 'test')
                """,
                (
                    label_id,
                    market_id,
                    (first_decision + timedelta(minutes=30)).isoformat(),
                    int(index % 2 == 0),
                    cursor.lastrowid,
                    label_available.isoformat(),
                    label_available.isoformat(),
                ),
            )
            for snapshot in range(snapshots_per_label):
                signal_id = f"signal-{index}-{snapshot}"
                decision = first_decision + timedelta(minutes=snapshot)
                connection.execute(
                    """
                    INSERT INTO analysis_signals (
                        signal_id, market_id, asset, status, reasons,
                        observed_at, received_at
                    ) VALUES (?, ?, 'BTC', 'analyzed', '[]', ?, ?)
                    """,
                    (signal_id, market_id, decision.isoformat(), decision.isoformat()),
                )
                feature = {
                    "estimated_probability": str(
                        0.2 + index * 0.2 + snapshot * 0.001
                    ),
                    "yes_midpoint": str(0.25 + index * 0.15),
                    "outcome_yes": index % 2 == 0,
                }
                serialized = json.dumps(
                    feature, sort_keys=True, separators=(",", ":")
                )
                items.append(
                    {
                        "ordinal": ordinal,
                        "signal_id": signal_id,
                        "label_id": label_id,
                        "decision_at": decision.isoformat(),
                        "label_available_at": label_available.isoformat(),
                        "feature_payload": serialized,
                        "feature_hash": hashlib.sha256(
                            serialized.encode()
                        ).hexdigest(),
                        "input_manifest_hash": "test",
                    }
                )
                ordinal += 1
    repository.seal_replay_dataset(
        dataset_id=dataset_id,
        name=dataset_id,
        manifest_hash="test",
        config={},
        source_version="test",
        created_at=NOW,
        items=tuple(items),
    )
    return repository, dataset_id
