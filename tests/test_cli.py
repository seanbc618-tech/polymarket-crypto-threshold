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
    assert root.exit_code == 0
    assert analyze.exit_code == 0
    assert dashboard.exit_code == 0
    assert shadow.exit_code == 0
    assert "--market" in analyze.output
    assert "market-prob" not in analyze.output
    assert "read-only research dashboard" in dashboard.output
    assert "--duration-hours" in shadow.output


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
