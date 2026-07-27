"""Immutable replay and leakage-safe walk-forward calibration tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_threshold.config import Settings
from crypto_threshold.domain.prices import KlineSeries
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


def test_training_replay_freezes_earliest_eligible_unique_labels(
    tmp_path: Path,
) -> None:
    _database, repository, label_ids = _daily_replay_candidates(
        tmp_path, label_count=3
    )

    service = ReplayService(repository, clock=lambda: TARGET + timedelta(minutes=10))
    built = service.build("training-v1", training_label_count=2)

    assert built.unique_label_count == 2
    assert built.training_cutoff_at == TARGET + timedelta(minutes=3)
    assert built.training_cutoff_label_id == label_ids[1]
    assert service.verify(built.dataset_id).ok

    dataset = repository.get_replay_dataset(built.dataset_id)
    assert dataset is not None
    config = json.loads(str(dataset["config_json"]))
    assert config["selection"] == {
        "mode": "first_n_eligible_labels",
        "requested_unique_label_count": 2,
        "selected_unique_label_count": 2,
        "selected_labels": [
            {
                "label_available_at": (
                    TARGET + timedelta(minutes=2)
                ).isoformat(),
                "label_id": label_ids[0],
            },
            {
                "label_available_at": (
                    TARGET + timedelta(minutes=3)
                ).isoformat(),
                "label_id": label_ids[1],
            },
        ],
        "training_cutoff": {
            "label_available_at": (TARGET + timedelta(minutes=3)).isoformat(),
            "label_id": label_ids[1],
        },
    }
    replay_labels = {
        str(row["label_id"])
        for row in repository.replay_item_rows(built.dataset_id)
    }
    assert replay_labels == set(label_ids[:2])
    assert any(
        reason.endswith(":label_after_training_cutoff")
        for reason in built.rejection_reasons
    )


def test_training_replay_refuses_to_seal_below_requested_unique_label_count(
    tmp_path: Path,
) -> None:
    database, repository, _label_ids = _daily_replay_candidates(
        tmp_path, label_count=2
    )

    with pytest.raises(
        ValueError,
        match="training replay requires 3 eligible unique labels; found 2",
    ):
        ReplayService(repository).build("too-short", training_label_count=3)

    assert repository.table_count("replay_datasets") == 0
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM replay_items").fetchone()[0] == 0


def test_replay_plan_reports_pending_training_boundary_without_writes(
    tmp_path: Path,
) -> None:
    _database, repository, _label_ids = _daily_replay_candidates(
        tmp_path, label_count=2
    )

    plan = ReplayService(repository).plan(training_label_count=3)

    assert not plan.ready
    assert plan.eligible_item_count == 2
    assert plan.eligible_unique_label_count == 2
    assert plan.selected_unique_label_count == 2
    assert plan.training_cutoff_at is None
    assert plan.training_cutoff_label_id is None
    assert repository.table_count("replay_datasets") == 0


def test_replay_verify_rejects_inconsistent_v2_selection_manifest(
    tmp_path: Path,
) -> None:
    _database, repository, _label_ids = _daily_replay_candidates(
        tmp_path, label_count=1
    )
    service = ReplayService(repository)
    built = service.build("valid-v2")
    dataset = repository.get_replay_dataset(built.dataset_id)
    assert dataset is not None
    config = json.loads(str(dataset["config_json"]))

    malformed_config = {
        **config,
        "selection": {
            **config["selection"],
            "selected_labels": [],
            "selected_unique_label_count": 0,
        },
    }
    items = [dict(row) for row in repository.replay_item_rows(built.dataset_id)]
    copied_items = tuple(
        {
            "ordinal": item["ordinal"],
            "signal_id": item["signal_id"],
            "label_id": item["label_id"],
            "decision_at": item["decision_at"],
            "label_available_at": item["label_available_at"],
            "feature_payload": item["feature_payload"],
            "feature_hash": item["feature_hash"],
            "input_manifest_hash": item["input_manifest_hash"],
        }
        for item in items
    )
    repository.seal_replay_dataset(
        dataset_id="malformed-selection",
        name="malformed-selection",
        manifest_hash="intentionally-not-the-focus",
        config=malformed_config,
        source_version="replay-manifest-v2",
        created_at=TARGET + timedelta(minutes=20),
        items=copied_items,
    )

    verified = service.verify("malformed-selection")

    assert not verified.ok
    assert "selected_label_manifest_mismatch" in verified.reasons


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


def _daily_replay_candidates(
    tmp_path: Path,
    *,
    label_count: int,
) -> tuple[Database, Repository, tuple[str, ...]]:
    database = Database(tmp_path / f"daily-candidates-{label_count}.db")
    database.initialize()
    repository = Repository(database)
    settings = Settings(DATABASE_PATH=str(database.path), _env_file=None)
    label_ids: list[str] = []

    for index in range(label_count):
        market_id = f"market-{index}"
        payload = make_market_payload(
            id=market_id,
            eventId=f"event-{index}",
            conditionId=f"condition-{index}",
            slug=f"bitcoin-above-100000-july-23-2026-{index}",
        )
        workflow = MarketWorkflowService(
            client=FakePolymarketClient(payload),
            repository=repository,
            binance=FakeBinanceProvider(),
            coinbase=FakeCoinbaseProvider(),
            settings=settings,
            clock=lambda: NOW,
        )
        signal = workflow.analyze(market_id)
        assert signal.threshold is not None
        label_available_at = TARGET + timedelta(minutes=index + 2)
        label = SettlementService(
            repository=repository,
            binance=_ReplaySettlementBinance(  # type: ignore[arg-type]
                signal.threshold + Decimal("1"), received_at=label_available_at
            ),
            clock=lambda: TARGET + timedelta(minutes=5),
        ).settle_market(market_id)
        label_ids.append(label.label_id)

    return database, repository, tuple(label_ids)


class _ReplaySettlementBinance(SettlementBinance):
    def __init__(self, close: Decimal, *, received_at: datetime) -> None:
        super().__init__(close)
        self.received_at = received_at

    def get_klines(
        self,
        asset: str,
        interval: str,
        limit: int,
        *,
        start_time: object,
        end_time: object,
    ) -> KlineSeries:
        series = super().get_klines(
            asset,
            interval,
            limit,
            start_time=start_time,
            end_time=end_time,
        )
        return replace(series, received_at=self.received_at)
