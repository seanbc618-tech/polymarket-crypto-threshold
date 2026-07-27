"""Mechanical Phase 2 acceptance service and CLI tests."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from crypto_threshold.cli import app
from crypto_threshold.config import Settings
from crypto_threshold.services.calibration_service import (
    CALIBRATION_METHOD,
    CALIBRATION_MODEL_VERSION,
    CALIBRATION_SOURCE_VERSION,
)
from crypto_threshold.services.market_workflow_service import MarketWorkflowService
from crypto_threshold.services.phase2_acceptance_service import (
    MAX_SHADOW_GAP_SECONDS,
    MIN_CHRONOLOGICAL_TRAIN_LABELS,
    MIN_SHADOW_HOURS,
    VERDICT_ACCEPTED,
    VERDICT_PENDING,
    Phase2AcceptanceService,
    _walk_forward_evidence,
)
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

runner = CliRunner()


def test_empty_initialized_db_is_pending_not_accepted(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    assert report.verdict == VERDICT_PENDING
    assert not report.accepted
    failed = {check.name for check in report.checks if not check.ok}
    assert "replay_dataset" in failed
    assert "chronological_train_and_oos" in failed
    assert "calibration_metrics" in failed
    assert "shadow_72h_coverage" in failed
    assert "cycle_rest_rejection_paper_evidence" in failed
    assert "external_schema_drift_monitoring" in failed
    assert "binance_websocket_evidence" in failed
    assert "schema_integrity" not in failed
    assert "no_trading_mutation_surface" not in failed


def test_missing_db_refuses_without_speculation(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    try:
        Phase2AcceptanceService.from_db_path(missing)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError as exc:
        assert "does not exist" in str(exc)


def test_non_sqlite_file_is_cli_inspection_error(tmp_path: Path) -> None:
    invalid = tmp_path / "not-sqlite.db"
    invalid.write_text("not a sqlite database", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "phase2-acceptance",
            "--db",
            str(invalid),
            "--output",
            str(tmp_path / "report.md"),
        ],
    )
    assert result.exit_code == 2
    assert "Phase 2 acceptance failed" in result.output
    assert not (tmp_path / "report.md").exists()


def test_existing_db_is_opened_read_only_and_cannot_be_report_target(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "readonly.db")
    database.initialize()
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    service = Phase2AcceptanceService.from_db_path(database.path)
    assert service.repository.database.read_only
    report = service.evaluate()
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before
    try:
        service.write_report(report, database.path)
        raise AssertionError("expected output/database collision to fail")
    except ValueError as exc:
        assert "must not overwrite" in str(exc)


def test_forbidden_trading_table_fails_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "orders.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute("CREATE TABLE orders (id TEXT PRIMARY KEY)")
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    trading = next(
        check for check in report.checks if check.name == "no_trading_mutation_surface"
    )
    assert not trading.ok
    assert "orders" in trading.detail
    assert report.verdict == VERDICT_PENDING


def test_full_evidence_db_is_accepted(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "accepted.db")
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    assert report.verdict == VERDICT_ACCEPTED, {
        check.name: check.detail for check in report.checks if not check.ok
    }
    assert report.accepted
    assert all(check.ok for check in report.checks)


def test_historical_daily_schema_is_accepted_without_settlement_attempts(
    tmp_path: Path,
) -> None:
    for version in (3, 4):
        database = _full_evidence_db(tmp_path / f"schema-v{version}.db")
        with database.transaction() as connection:
            connection.execute("DROP TABLE settlement_attempts")
            connection.execute(
                "UPDATE schema_meta SET version = ? WHERE id = 1",
                (version,),
            )

        report = Phase2AcceptanceService(Repository(database)).evaluate()
        schema = next(
            check for check in report.checks if check.name == "schema_integrity"
        )
        assert schema.ok, schema.detail
        assert report.verdict == VERDICT_ACCEPTED


def test_current_schema_still_requires_settlement_attempts(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "schema-v5-missing-attempts.db")
    with database.transaction() as connection:
        connection.execute("DROP TABLE settlement_attempts")

    report = Phase2AcceptanceService(Repository(database)).evaluate()
    schema = next(check for check in report.checks if check.name == "schema_integrity")
    assert not schema.ok
    assert "settlement_attempts" in schema.detail
    assert report.verdict == VERDICT_PENDING


def test_unknown_schema_version_fails_closed(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "schema-unknown.db")
    with database.transaction() as connection:
        connection.execute("UPDATE schema_meta SET version = 6 WHERE id = 1")

    report = Phase2AcceptanceService(Repository(database)).evaluate()
    schema = next(check for check in report.checks if check.name == "schema_integrity")
    assert not schema.ok
    assert "schema_version=6" in schema.detail
    assert report.verdict == VERDICT_PENDING


def test_report_markdown_and_cli_write_pending(tmp_path: Path) -> None:
    database = Database(tmp_path / "cli.db")
    database.initialize()
    output = tmp_path / "report.md"
    result = runner.invoke(
        app,
        [
            "phase2-acceptance",
            "--db",
            str(database.path),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 1
    assert "PENDING/NOT ACCEPTED" in result.output
    text = output.read_text(encoding="utf-8")
    assert "PENDING/NOT ACCEPTED" in text
    assert "replay_dataset" in text
    assert "Do not treat software completeness" in text


def test_cli_accepted_exit_zero(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "cli-accepted.db")
    output = tmp_path / "accepted.md"
    result = runner.invoke(
        app,
        [
            "phase2-acceptance",
            "--db",
            str(database.path),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ACCEPTED" in result.output
    assert "PENDING" not in result.output
    assert VERDICT_ACCEPTED in output.read_text(encoding="utf-8")


def test_metrics_must_be_finite(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "metrics.db")
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE calibration_runs
            SET metrics_json = ?
            """,
            (
                json.dumps(
                    {
                        "raw": {
                            "brier": math.inf,
                            "log_loss": 0.1,
                            "ece": 0.1,
                        },
                        "calibrated": {
                            "brier": 0.1,
                            "log_loss": 0.1,
                            "ece": 0.1,
                        },
                        "market_midpoint_baseline": {
                            "brier": 0.1,
                            "log_loss": 0.1,
                            "ece": 0.1,
                        },
                    }
                ),
            ),
        )
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    metrics = next(check for check in report.checks if check.name == "calibration_metrics")
    assert not metrics.ok
    assert report.verdict == VERDICT_PENDING


def test_shadow_under_72h_is_not_accepted(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "short-shadow.db")
    with database.transaction() as connection:
        connection.execute("DELETE FROM shadow_cycles")
        _insert_shadow_cycle(
            connection,
            cycle_id="shadow:short-1",
            started=NOW,
            completed=NOW + timedelta(hours=1),
            status="complete_rest_fallback",
            drained_ticks=1,
            generation=2,
        )
        _insert_shadow_cycle(
            connection,
            cycle_id="shadow:short-2",
            started=NOW + timedelta(hours=2),
            completed=NOW + timedelta(hours=3),
            status="complete_rest_fallback",
            drained_ticks=1,
            generation=2,
        )
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    shadow = next(check for check in report.checks if check.name == "shadow_72h_coverage")
    assert not shadow.ok
    assert str(MIN_SHADOW_HOURS) in shadow.detail
    assert report.verdict == VERDICT_PENDING


def test_sparse_shadow_endpoints_are_not_continuous_evidence(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "sparse-shadow.db")
    with database.transaction() as connection:
        connection.execute("DELETE FROM shadow_cycles")
        _insert_shadow_cycle(
            connection,
            cycle_id="shadow:sparse-1",
            started=NOW,
            completed=NOW + timedelta(seconds=1),
            status="complete_rest_fallback",
            drained_ticks=1,
            generation=1,
        )
        _insert_shadow_cycle(
            connection,
            cycle_id="shadow:sparse-2",
            started=NOW + timedelta(hours=MIN_SHADOW_HOURS),
            completed=NOW + timedelta(hours=MIN_SHADOW_HOURS, seconds=1),
            status="complete_rest_fallback",
            drained_ticks=1,
            generation=2,
        )
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    shadow = next(check for check in report.checks if check.name == "shadow_72h_coverage")
    assert not shadow.ok
    assert shadow.evidence["oversized_gaps"]
    assert report.verdict == VERDICT_PENDING


def test_completed_shadow_segment_survives_a_later_collection_gap(
    tmp_path: Path,
) -> None:
    database = _full_evidence_db(tmp_path / "continued-shadow.db")
    with database.transaction() as connection:
        _insert_shadow_cycle(
            connection,
            cycle_id="shadow:later-session",
            started=NOW + timedelta(hours=80),
            completed=NOW + timedelta(hours=80, seconds=1),
            status="complete_rest_fallback",
            drained_ticks=1,
            generation=2,
        )

    report = Phase2AcceptanceService(Repository(database)).evaluate()
    shadow = next(check for check in report.checks if check.name == "shadow_72h_coverage")
    assert shadow.ok, shadow.detail
    assert shadow.evidence["oversized_gaps"]
    assert shadow.evidence["qualifying_segments"]
    assert report.verdict == VERDICT_ACCEPTED


def test_one_overlong_cycle_cannot_stand_in_for_monitoring(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "overlong-shadow.db")
    with database.transaction() as connection:
        connection.execute("DELETE FROM shadow_cycles")
        _insert_shadow_cycle(
            connection,
            cycle_id="shadow:stuck",
            started=NOW,
            completed=NOW + timedelta(hours=MIN_SHADOW_HOURS),
            status="complete_rest_fallback",
            drained_ticks=1,
            generation=2,
        )
        _insert_shadow_cycle(
            connection,
            cycle_id="shadow:after",
            started=NOW + timedelta(hours=MIN_SHADOW_HOURS),
            completed=NOW + timedelta(hours=MIN_SHADOW_HOURS, seconds=1),
            status="complete_rest_fallback",
            drained_ticks=1,
            generation=2,
        )
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    shadow = next(check for check in report.checks if check.name == "shadow_72h_coverage")
    assert not shadow.ok
    assert shadow.evidence["overlong_cycles"]
    assert report.verdict == VERDICT_PENDING


def test_calibration_claims_must_match_actual_replay_rows(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "lying-counts.db")
    repository = Repository(database)
    one_item = repository.get_replay_dataset("phase2-one-item-fixture")
    assert one_item is not None
    with database.transaction() as connection:
        connection.execute("DELETE FROM calibration_runs")
        connection.execute(
            """
            INSERT INTO calibration_runs (
                run_id, dataset_id, status, method, bins, min_train_size,
                sample_count, evaluated_count, metrics_json, rejection_reason,
                model_version, source_version, started_at, completed_at
            ) VALUES ('calibration:lying', ?, 'complete', ?, 10, 30, 31, 1, ?,
                      NULL, ?, ?, ?, ?)
            """,
            (
                str(one_item["dataset_id"]),
                CALIBRATION_METHOD,
                json.dumps(_complete_metrics()),
                CALIBRATION_MODEL_VERSION,
                CALIBRATION_SOURCE_VERSION,
                NOW.isoformat(),
                (NOW + timedelta(minutes=1)).isoformat(),
            ),
        )
    report = Phase2AcceptanceService(repository).evaluate()
    training = next(
        check for check in report.checks if check.name == "chronological_train_and_oos"
    )
    metrics = next(check for check in report.checks if check.name == "calibration_metrics")
    assert not training.ok
    assert not metrics.ok
    assert "sample_count_mismatch" in training.evidence["rejected_runs"][0]["reasons"]
    assert (
        "missing_frozen_training_reference"
        in training.evidence["rejected_runs"][0]["reasons"]
    )
    assert report.verdict == VERDICT_PENDING


def test_repeated_snapshots_do_not_impersonate_unique_training_labels() -> None:
    rows = [
        {
            "ordinal": index,
            "label_id": "label:one-outcome",
            "decision_at": (NOW + timedelta(minutes=index)).isoformat(),
            "label_available_at": (NOW + timedelta(hours=1)).isoformat(),
        }
        for index in range(MIN_CHRONOLOGICAL_TRAIN_LABELS + 1)
    ]
    evidence = _walk_forward_evidence(
        rows,
        min_train=MIN_CHRONOLOGICAL_TRAIN_LABELS,
    )
    assert evidence["sample_count"] == 1
    assert evidence["evaluated_count"] == 0
    assert evidence["issues"] == []


def test_drained_tick_counter_alone_is_not_binance_stream_evidence(
    tmp_path: Path,
) -> None:
    database = _full_evidence_db(tmp_path / "counter-only.db")
    health = json.dumps(
        {
            "polymarket": {"status": "disabled"},
            "binance_reference": {
                "status": "connected",
                "detail": {"generation": 2},
                "drained_ticks": 1,
            },
        }
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE shadow_cycles SET stream_health_json = ?",
            (health,),
        )
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    stream = next(
        check for check in report.checks if check.name == "binance_websocket_evidence"
    )
    assert not stream.ok
    assert "closed_1m_tick" in stream.evidence["missing"]
    assert report.verdict == VERDICT_PENDING


def test_schema_drift_evidence_blocks_acceptance(tmp_path: Path) -> None:
    database = _full_evidence_db(tmp_path / "schema-drift.db")
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT cycle_id, stream_health_json FROM shadow_cycles LIMIT 1"
        ).fetchone()
        assert row is not None
        health = json.loads(str(row["stream_health_json"]))
        health["schema_drift"]["status"] = "drift_detected"
        health["schema_drift"]["issues"] = [
            {
                "payload_id": 1,
                "contract": "gamma/market",
                "code": "missing_question",
            }
        ]
        connection.execute(
            "UPDATE shadow_cycles SET stream_health_json = ? WHERE cycle_id = ?",
            (json.dumps(health), row["cycle_id"]),
        )
    report = Phase2AcceptanceService(Repository(database)).evaluate()
    drift = next(
        check
        for check in report.checks
        if check.name == "external_schema_drift_monitoring"
    )
    assert not drift.ok
    assert drift.evidence["failed_cycles"]
    assert report.verdict == VERDICT_PENDING


def _full_evidence_db(path: Path) -> Database:
    """Build the minimum concrete evidence set that satisfies every gate."""
    database = Database(path)
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
    one_item = ReplayService(
        repository, clock=lambda: TARGET + timedelta(minutes=3)
    ).build("phase2-one-item-fixture")
    assert one_item.item_count == 1
    assert ReplayService(repository).verify(one_item.dataset_id).ok

    _clone_chronological_replay_history(database, signal.signal_id)
    replay_service = ReplayService(
        repository, clock=lambda: TARGET + timedelta(days=61)
    )
    training = replay_service.build(
        "phase2-training-fixture",
        training_label_count=MIN_CHRONOLOGICAL_TRAIN_LABELS,
    )
    assert training.unique_label_count == MIN_CHRONOLOGICAL_TRAIN_LABELS
    assert ReplayService(repository).verify(training.dataset_id).ok
    built = replay_service.build(
        "phase2-acceptance-fixture",
        training_dataset=training.dataset_id,
    )
    assert built.item_count == MIN_CHRONOLOGICAL_TRAIN_LABELS + 1
    assert ReplayService(repository).verify(built.dataset_id).ok

    metrics = _complete_metrics()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO calibration_runs (
                run_id, dataset_id, status, method, bins, min_train_size,
                sample_count, evaluated_count, metrics_json, rejection_reason,
                model_version, source_version, started_at, completed_at
            ) VALUES (?, ?, 'complete', ?, 10, ?, ?, 1, ?, NULL, ?, ?, ?, ?)
            """,
            (
                "calibration:acceptance",
                built.dataset_id,
                CALIBRATION_METHOD,
                MIN_CHRONOLOGICAL_TRAIN_LABELS,
                MIN_CHRONOLOGICAL_TRAIN_LABELS + 1,
                json.dumps(metrics),
                CALIBRATION_MODEL_VERSION,
                CALIBRATION_SOURCE_VERSION,
                NOW.isoformat(),
                (NOW + timedelta(minutes=1)).isoformat(),
            ),
        )
        # Structured rejection evidence separate from the analyzed replay signal.
        connection.execute(
            """
            INSERT INTO analysis_signals (
                signal_id, market_id, asset, status, reasons, observed_at, received_at
            ) VALUES (?, 'market-1', 'BTC', 'rejected', ?, ?, ?)
            """,
            (
                "signal:rejected-evidence",
                json.dumps(["incomplete_executable_asks"]),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO paper_ledger (
                entry_id, signal_id, market_id, policy_version, action, outcome,
                status, size_usdc, reasons, observed_at, received_at, source_version
            ) VALUES (?, ?, 'market-1', 'paper-v1', 'skip', NULL, 'skipped', '10',
                      ?, ?, ?, 'paper-ledger-v1')
            """,
            (
                "paper:acceptance",
                signal.signal_id,
                json.dumps(["net_ev_below_threshold"]),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        cycle_count = int(
            MIN_SHADOW_HOURS * 3600 / MAX_SHADOW_GAP_SECONDS
        ) + 1
        for index in range(cycle_count):
            started = NOW + timedelta(seconds=index * MAX_SHADOW_GAP_SECONDS)
            _insert_shadow_cycle(
                connection,
                cycle_id=f"shadow:{index:04d}",
                started=started,
                completed=started + timedelta(seconds=1),
                status="complete_rest_fallback",
                drained_ticks=1,
                generation=1 if index == 0 else 2,
            )
    return database


def _clone_chronological_replay_history(
    database: Database,
    original_signal_id: str,
) -> None:
    with database.transaction() as connection:
        label = connection.execute(
            "SELECT * FROM settlement_labels WHERE market_id = 'market-1'"
        ).fetchone()
        assert label is not None
        for index in range(1, MIN_CHRONOLOGICAL_TRAIN_LABELS + 1):
            market_id = f"market-{index + 1}"
            signal_id = f"signal:history:{index:02d}"
            decision_at = NOW + timedelta(days=index * 2)
            target_at = decision_at + timedelta(hours=28)
            label_received_at = target_at + timedelta(minutes=2)
            connection.execute(
                """
                INSERT INTO markets (market_id, question, raw_payload)
                VALUES (?, ?, '{}')
                """,
                (market_id, f"Synthetic test fixture market {index}"),
            )
            connection.execute(
                """
                INSERT INTO analysis_signals (
                    signal_id, market_id, analysis_run_id, asset, threshold,
                    deadline, estimated_probability, probability_low,
                    probability_high, market_probability, edge, yes_midpoint,
                    no_midpoint, yes_ask_vwap, no_ask_vwap, target_size_usdc,
                    fee_rate, yes_fee_per_share, no_fee_per_share,
                    yes_spread_cost, no_spread_cost, yes_slippage_cost,
                    no_slippage_cost, yes_net_ev, no_net_ev, selected_outcome,
                    net_ev, status, model_name, model_version, confidence,
                    reasons, input_payload_max_id, observed_at, received_at,
                    source_version
                )
                SELECT
                    ?, ?, analysis_run_id, asset, threshold, ?,
                    estimated_probability, probability_low, probability_high,
                    market_probability, edge, yes_midpoint, no_midpoint,
                    yes_ask_vwap, no_ask_vwap, target_size_usdc, fee_rate,
                    yes_fee_per_share, no_fee_per_share, yes_spread_cost,
                    no_spread_cost, yes_slippage_cost, no_slippage_cost,
                    yes_net_ev, no_net_ev, selected_outcome, net_ev, status,
                    model_name, model_version, confidence, reasons,
                    input_payload_max_id, ?, ?, source_version
                FROM analysis_signals WHERE signal_id = ?
                """,
                (
                    signal_id,
                    market_id,
                    target_at.isoformat(),
                    decision_at.isoformat(),
                    decision_at.isoformat(),
                    original_signal_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO analysis_signal_inputs (signal_id, payload_id, input_role)
                SELECT ?, payload_id, input_role
                FROM analysis_signal_inputs WHERE signal_id = ?
                """,
                (signal_id, original_signal_id),
            )
            connection.execute(
                """
                INSERT INTO settlement_labels (
                    label_id, market_id, target_time_utc, provider, pair,
                    candle_interval, price_field, exact_operator, strike,
                    observed_value, outcome_yes, payload_id, observed_at,
                    received_at, source_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"label:history:{index:02d}",
                    market_id,
                    target_at.isoformat(),
                    label["provider"],
                    label["pair"],
                    label["candle_interval"],
                    label["price_field"],
                    label["exact_operator"],
                    label["strike"],
                    label["observed_value"],
                    label["outcome_yes"],
                    label["payload_id"],
                    (target_at + timedelta(minutes=1)).isoformat(),
                    label_received_at.isoformat(),
                    label["source_version"],
                ),
            )


def _complete_metrics() -> dict[str, dict[str, float]]:
    return {
        family: {"brier": 0.2, "log_loss": 0.5, "ece": 0.1}
        for family in ("raw", "calibrated", "market_midpoint_baseline")
    }


def _insert_shadow_cycle(
    connection: object,
    *,
    cycle_id: str,
    started: object,
    completed: object,
    status: str,
    drained_ticks: int,
    generation: int,
) -> None:
    tick_evidence = [
        {
            "provider": "binance",
            "pair": "BTCUSDT",
            "candle_interval": "1m",
            "price_field": "Close",
            "provider_timestamp": started.isoformat(),
            "received_at": completed.isoformat(),
            "fresh": True,
            "sequence": "1:1",
            "payload_hash": "0" * 64,
            "source_version": "binance-spot-sdk-stream-v1",
        }
    ] if drained_ticks else []
    stream_health = {
        "polymarket": {
            "status": "disabled",
            "detail": {"rest_fallback_active": True},
        },
        "schema_drift": {
            "status": "ok",
            "boundary_payload_id": 0,
            "last_payload_id": 1,
            "scanned_payload_count": 1,
            "contract_counts": {"gamma/market": 1},
            "source_versions": {"gamma/market": ["gamma-markets-v1"]},
            "issues": [],
            "source_version": "external-payload-schema-monitor-v1",
        },
        "binance_reference": {
            "status": "connected",
            "detail": {
                "generation": generation,
                "pairs": ["BTCUSDT", "ETHUSDT"],
                "source_version": "binance-spot-sdk-stream-v1",
            },
            "drained_ticks": drained_ticks,
            "drained_tick_evidence": tick_evidence,
        },
    }
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO shadow_cycles (
            cycle_id, mode, status, discovered_count, analyzed_count,
            paper_entered_count, paper_skipped_count, stream_health_json,
            reasons, source_version, started_at, completed_at
        ) VALUES (?, 'shadow', ?, 1, 1, 0, 1, ?, ?, 'shadow-monitor-v1', ?, ?)
        """,
        (
            cycle_id,
            status,
            json.dumps(stream_health),
            json.dumps(["rest_fallback_active"]),
            started.isoformat() if hasattr(started, "isoformat") else str(started),
            completed.isoformat() if hasattr(completed, "isoformat") else str(completed),
        ),
    )
