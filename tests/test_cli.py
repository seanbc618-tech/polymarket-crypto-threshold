"""CLI safety-contract tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from crypto_threshold.cli import app
from crypto_threshold.config import get_settings

runner = CliRunner()


def test_help_and_analyze_contract() -> None:
    root = runner.invoke(app, ["--help"])
    analyze = runner.invoke(app, ["analyze", "--help"])
    dashboard = runner.invoke(app, ["dashboard", "--help"])
    shadow = runner.invoke(app, ["shadow", "--help"])
    replay_build = runner.invoke(app, ["replay-build", "--help"])
    replay_plan = runner.invoke(app, ["replay-plan", "--help"])
    challenger_status = runner.invoke(app, ["short-challenger-status", "--help"])
    execution_blueprint = runner.invoke(app, ["execution-blueprint", "--help"])
    research_tooling = runner.invoke(app, ["research-tooling-status", "--help"])
    assert root.exit_code == 0
    assert analyze.exit_code == 0
    assert dashboard.exit_code == 0
    assert shadow.exit_code == 0
    assert replay_build.exit_code == 0
    assert replay_plan.exit_code == 0
    assert challenger_status.exit_code == 0
    assert execution_blueprint.exit_code == 0
    assert research_tooling.exit_code == 0
    assert "--market" in analyze.output
    assert "market-prob" not in analyze.output
    assert "read-only research dashboard" in dashboard.output
    assert "--duration-hours" in shadow.output
    assert "--training-dataset" in replay_build.output
    assert "--training-label-count" in replay_plan.output
    assert "--db" in challenger_status.output
    assert "non-executable" in execution_blueprint.output
    assert "R1/R2" in research_tooling.output


def test_execution_blueprint_cli_is_pinned_and_non_executable() -> None:
    result = runner.invoke(app, ["execution-blueprint"])
    assert result.exit_code == 0
    assert "REFERENCE BLUEPRINT ONLY" in result.output
    assert "v1.230.0" in result.output
    assert "IOC" in result.output
    assert "FAK" in result.output
    assert "live_submission=false" in result.output
    assert "submit/cancel/authenticated reconciliation not implemented" in result.output


def test_research_tooling_cli_reports_core_without_claiming_acceptance() -> None:
    result = runner.invoke(app, ["research-tooling-status"])
    assert result.exit_code == 0
    assert "R1 HFT replay" in result.output
    assert "REAL L2 TAPES PENDING" in result.output
    assert "R2 integrity" in result.output
    assert "SEALED CANDIDATE RUN PENDING" in result.output
    assert "LIVE NO-GO" in result.output
    assert "submission=false" in result.output


def test_init_db_creates_read_only_schema(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    result = runner.invoke(app, ["init-db", "--db-path", db_path])
    assert result.exit_code == 0
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "markets" in tables
    assert "external_payloads" in tables
    assert "analysis_signals" in tables
    assert "discovered_markets" not in tables
    assert "orders" not in tables


def test_doctor_checks_db_urls_providers_and_trading_mode(
    tmp_path: Path, monkeypatch: object
) -> None:
    db_path = tmp_path / "doctor.db"
    assert runner.invoke(app, ["init-db", "--db-path", str(db_path)]).exit_code == 0
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    get_settings.cache_clear()
    result = runner.invoke(app, ["doctor", "--no-network"])
    get_settings.cache_clear()
    assert result.exit_code == 0
    for check in (
        "database",
        "providers",
        "stream_mode",
        "phase2_mode",
        "gamma_url",
        "clob_url",
        "site_api_url",
        "binance_stream_url",
        "trading_mode",
    ):
        assert check in result.output


def test_doctor_fails_closed_for_missing_db(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "missing.db"))
    get_settings.cache_clear()
    result = runner.invoke(app, ["doctor", "--no-network"])
    get_settings.cache_clear()
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_doctor_fails_closed_when_trading_flag_enabled(
    tmp_path: Path, monkeypatch: object
) -> None:
    db_path = tmp_path / "unsafe.db"
    assert runner.invoke(app, ["init-db", "--db-path", str(db_path)]).exit_code == 0
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TRADING_DISABLED", "false")
    get_settings.cache_clear()
    result = runner.invoke(app, ["doctor", "--no-network"])
    get_settings.cache_clear()
    assert result.exit_code == 1
    assert "unsafe live flag enabled" in result.output


def test_doctor_fails_closed_for_non_shadow_or_user_stream(
    tmp_path: Path, monkeypatch: object
) -> None:
    db_path = tmp_path / "unsafe-stream.db"
    assert runner.invoke(app, ["init-db", "--db-path", str(db_path)]).exit_code == 0
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_STREAM_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_STREAM_SHADOW_MODE", "false")
    monkeypatch.setenv("POLYMARKET_STREAM_USER_CHANNEL_ENABLED", "true")
    get_settings.cache_clear()
    result = runner.invoke(app, ["doctor", "--no-network"])
    get_settings.cache_clear()
    assert result.exit_code == 1
    assert "unsafe stream configuration" in result.output


def test_shadow_is_disabled_by_default() -> None:
    get_settings.cache_clear()
    result = runner.invoke(app, ["shadow", "--once"])
    get_settings.cache_clear()
    assert result.exit_code == 2
    assert "disabled by default" in result.output


def test_shadow_rejects_conflicting_duration_modes() -> None:
    get_settings.cache_clear()
    result = runner.invoke(
        app,
        ["shadow", "--once", "--duration-hours", "72"],
    )
    get_settings.cache_clear()
    assert result.exit_code == 2
    assert "either --once or --duration-hours" in result.output


def test_shadow_rejects_private_key_from_process_environment(
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("SHADOW_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-secret-must-not-be-used")
    get_settings.cache_clear()
    result = runner.invoke(app, ["shadow", "--once"])
    get_settings.cache_clear()
    assert result.exit_code == 2
    assert "Unsafe shadow configuration" in result.output
    assert "test-secret" not in result.output


def test_short_shadow_requires_sealed_cex_model(
    monkeypatch: object,
) -> None:
    monkeypatch.setenv("SHADOW_ENABLED", "true")
    monkeypatch.setenv("SHADOW_CONTRACT_FAMILY", "short_updown")
    monkeypatch.setenv("CHAINLINK_REFERENCE_STREAM_ENABLED", "false")
    get_settings.cache_clear()
    result = runner.invoke(app, ["shadow", "--once"])
    get_settings.cache_clear()

    assert result.exit_code == 2
    assert "requires a valid sealed CEX model" in result.output


def test_short_challenger_status_reads_empty_r0_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "challenger-status.db"
    assert runner.invoke(app, ["init-db", "--db-path", str(db_path)]).exit_code == 0

    result = runner.invoke(
        app,
        ["short-challenger-status", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert "observations=0" in result.output
    assert "latency_replays=0" in result.output


def test_empty_replay_build_is_persisted_but_fails_acceptance(
    tmp_path: Path, monkeypatch: object
) -> None:
    db_path = tmp_path / "empty-replay.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    get_settings.cache_clear()
    result = runner.invoke(app, ["replay-build", "--name", "empty"])
    get_settings.cache_clear()
    assert result.exit_code == 2
    assert "cannot pass acceptance" in result.output
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status, item_count FROM replay_datasets WHERE name='empty'"
        ).fetchone() == ("sealed", 0)


def test_training_replay_cli_refuses_short_candidate_set_without_sealing(
    tmp_path: Path, monkeypatch: object
) -> None:
    db_path = tmp_path / "short-training-replay.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    get_settings.cache_clear()
    result = runner.invoke(
        app,
        [
            "replay-build",
            "--name",
            "training",
            "--training-label-count",
            "30",
        ],
    )
    get_settings.cache_clear()

    assert result.exit_code == 2
    assert "requires 30 eligible unique" in result.output
    assert "labels; found 0" in result.output
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM replay_datasets").fetchone()[0] == 0


def test_replay_plan_cli_is_pending_and_does_not_write_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "replay-plan.db"
    initialized = runner.invoke(app, ["init-db", "--db-path", str(db_path)])
    assert initialized.exit_code == 0
    modified_before = db_path.stat().st_mtime_ns

    result = runner.invoke(
        app,
        [
            "replay-plan",
            "--db",
            str(db_path),
            "--training-label-count",
            "30",
        ],
    )

    assert result.exit_code == 1
    assert "Replay plan PENDING" in result.output
    assert "eligible_unique_labels=0/30" in result.output
    assert db_path.stat().st_mtime_ns == modified_before
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM replay_datasets").fetchone()[0] == 0
