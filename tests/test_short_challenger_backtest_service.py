"""Read-only sealed R0 backtest tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from crypto_threshold.services.short_challenger_backtest_service import (
    ShortChallengerBacktestService,
    sha256_file,
)
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository

MODEL_VERSION = "cex-kline-chainlink-direction-v1+frozen-test"
SOURCE_VERSION = "short-challenger-r0-v1"


def test_backtest_event_balances_probabilities_and_recomputes_execution_pnl(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "frozen.db"
    writable = Database(db_path)
    writable.initialize()
    _seed_backtest_rows(writable)
    database_hash_before = sha256_file(db_path)

    service = ShortChallengerBacktestService(
        Repository(Database(db_path, read_only=True)),
        minimum_event_groups=2,
        minimum_dates=2,
        required_assets=("BTC", "ETH"),
        minimum_groups_per_asset=1,
        clock=lambda: datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    report = service.run(
        model_version=MODEL_VERSION,
        database_sha256=database_hash_before,
    )
    repeated = service.run(
        model_version=MODEL_VERSION,
        database_sha256=database_hash_before,
    )

    assert sha256_file(db_path) == database_hash_before
    assert report.input_manifest_hash == repeated.input_manifest_hash
    assert report.report_manifest_hash == repeated.report_manifest_hash
    assert report.coverage.passed
    assert report.coverage.event_group_count == 2
    assert report.coverage.date_count == 2
    assert report.coverage.asset_group_counts == {"BTC": 1, "ETH": 1}
    assert report.integrity.passed
    assert not report.refit_performed
    assert not report.promotion_allowed
    assert not report.live_trading_allowed

    checkpoint = report.checkpoint_results[0]
    assert checkpoint.paired_contract_count == 3
    assert checkpoint.paired_event_group_count == 2
    assert checkpoint.model_metrics is not None
    assert checkpoint.market_baseline_metrics is not None
    assert checkpoint.model_metrics.brier == pytest.approx(0.325)
    assert checkpoint.market_baseline_metrics.brier == pytest.approx(0.21)
    assert not checkpoint.model_beats_market_brier

    execution = report.execution_results[0]
    assert execution.settled_entry_count == 3
    assert execution.wins == 2
    assert execution.losses == 1
    assert str(execution.total_pnl_usdc) == "7"
    assert str(execution.total_fees_usdc) == "3"
    assert execution.pnl_recompute_mismatch_count == 0
    assert execution.roi_on_filled_stake is not None
    assert float(execution.roi_on_filled_stake) == pytest.approx(7 / 30)


def test_backtest_requires_read_only_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "writable.db")
    database.initialize()

    with pytest.raises(ValueError, match="read-only"):
        ShortChallengerBacktestService(Repository(database))


def _seed_backtest_rows(database: Database) -> None:
    day_one = datetime(2026, 7, 30, 12, 5, tzinfo=UTC)
    day_two = datetime(2026, 7, 31, 12, 5, tzinfo=UTC)
    rows = (
        ("m1", "BTC", day_one, "0.9", "0.6", 1, "YES"),
        ("m2", "BTC", day_one, "0.1", "0.6", 0, "NO"),
        ("m3", "ETH", day_two, "0.8", "0.4", 0, "YES"),
    )
    with database.transaction() as connection:
        for index, (market_id, asset, target, model, market, outcome, side) in enumerate(
            rows,
            start=1,
        ):
            signal_id = f"signal-{index}"
            observation_id = f"observation-{index}"
            label_id = f"label-{index}"
            received = target - timedelta(minutes=3)
            connection.execute(
                "INSERT INTO markets (market_id, question) VALUES (?, ?)",
                (market_id, f"{asset} Up or Down?"),
            )
            connection.execute(
                """
                INSERT INTO analysis_signals (signal_id, market_id, asset)
                VALUES (?, ?, ?)
                """,
                (signal_id, market_id, asset),
            )
            payload_id = connection.execute(
                """
                INSERT INTO external_payloads (
                    market_id, source, payload_kind, received_at,
                    source_version, raw_payload
                ) VALUES (?, 'chainlink', 'settlement', ?, 'test', '{}')
                """,
                (market_id, (target + timedelta(minutes=1)).isoformat()),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO settlement_labels (
                    label_id, market_id, target_time_utc, provider, pair,
                    candle_interval, price_field, exact_operator, strike,
                    observed_value, outcome_yes, payload_id, observed_at,
                    received_at, source_version, contract_family
                ) VALUES (?, ?, ?, 'chainlink', ?, '5m', 'value', '>=', '0',
                          '1', ?, ?, ?, ?, 'test', 'short_updown')
                """,
                (
                    label_id,
                    market_id,
                    target.isoformat(),
                    f"{asset}/USD",
                    outcome,
                    payload_id,
                    target.isoformat(),
                    (target + timedelta(minutes=1)).isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO short_challenger_observations (
                    observation_id, signal_id, market_id, asset, target_time_utc,
                    checkpoint_lead_seconds, checkpoint_at, model_version,
                    model_probability, market_yes_midpoint, target_size_usdc,
                    selected_outcome, status, reasons, observed_at, received_at,
                    source_version
                ) VALUES (?, ?, ?, ?, ?, 180, ?, ?, ?, ?, '10', ?, 'captured',
                          '[]', ?, ?, ?)
                """,
                (
                    observation_id,
                    signal_id,
                    market_id,
                    asset,
                    target.isoformat(),
                    received.isoformat(),
                    MODEL_VERSION,
                    model,
                    market,
                    side,
                    received.isoformat(),
                    received.isoformat(),
                    SOURCE_VERSION,
                ),
            )
            won = (side == "YES" and bool(outcome)) or (side == "NO" and not outcome)
            pnl = "9" if won else "-11"
            connection.execute(
                """
                INSERT INTO short_latency_replays (
                    replay_id, observation_id, latency_ms, actual_latency_ms,
                    outcome, action, status, size_usdc, entry_vwap, shares,
                    total_fee, label_id, outcome_yes, payout_usdc, pnl_usdc,
                    reasons, requested_at, sampled_at, settled_at, source_version
                ) VALUES (?, ?, 0, 0, ?, 'enter', 'settled', '10', '0.5', '20',
                          '1', ?, ?, ?, ?, '[]', ?, ?, ?, 'test-replay')
                """,
                (
                    f"replay-{index}",
                    observation_id,
                    side,
                    label_id,
                    outcome,
                    "20" if won else "0",
                    pnl,
                    received.isoformat(),
                    received.isoformat(),
                    (target + timedelta(minutes=1)).isoformat(),
                ),
            )
