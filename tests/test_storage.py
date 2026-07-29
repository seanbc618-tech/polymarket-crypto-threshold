"""SQLite migration, transaction, and ownership tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from crypto_threshold.storage.db import SCHEMA_VERSION, Database


def test_fresh_schema_has_single_market_owner_and_no_trade_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.db")
    database.initialize()
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "markets" in tables
        assert "analysis_signal_inputs" in tables
        assert "settlement_labels" in tables
        assert "replay_datasets" in tables
        assert "calibration_runs" in tables
        assert "paper_ledger" in tables
        assert "short_challenger_observations" in tables
        assert "short_latency_replays" in tables
        assert "shadow_cycles" in tables
        assert "discovered_markets" not in tables
        assert "order_intents" not in tables
        assert "orders" not in tables
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_legacy_schema_migrates_and_preserves_signal(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE markets (
            market_id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            slug TEXT,
            description TEXT,
            active INTEGER DEFAULT 1,
            closed INTEGER DEFAULT 0,
            end_date TEXT,
            outcomes TEXT,
            tokens TEXT,
            raw_payload TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE analysis_signals (
            signal_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL REFERENCES markets(market_id),
            asset TEXT NOT NULL,
            threshold TEXT NOT NULL,
            deadline TEXT,
            estimated_probability REAL,
            probability_low REAL,
            probability_high REAL,
            market_probability REAL,
            edge REAL,
            model_name TEXT,
            confidence REAL,
            reasons TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO markets (market_id, question) VALUES ('old-market', 'old question');
        INSERT INTO analysis_signals (
            signal_id, market_id, asset, threshold, reasons
        ) VALUES ('old-signal', 'old-market', 'BTC', '100000', '[]');
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    with database.connect() as migrated:
        version = migrated.execute(
            "SELECT version FROM schema_meta WHERE id = 1"
        ).fetchone()[0]
        threshold = next(
            row for row in migrated.execute("PRAGMA table_info(analysis_signals)")
            if row["name"] == "threshold"
        )
        row = migrated.execute(
            "SELECT status, source_version FROM analysis_signals WHERE signal_id='old-signal'"
        ).fetchone()
    assert version == SCHEMA_VERSION
    assert threshold["notnull"] == 0
    assert row["status"] == "rejected"
    assert row["source_version"] == "market-workflow-v1"


def test_transaction_rolls_back_all_writes(tmp_path: Path) -> None:
    database = Database(tmp_path / "rollback.db")
    database.initialize()
    with pytest.raises(RuntimeError, match="stop"):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO markets (market_id, question) VALUES ('rolled-back', 'test')"
            )
            raise RuntimeError("stop")
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM markets WHERE market_id='rolled-back'"
        ).fetchone()[0]
    assert count == 0


def test_broken_ddl_transaction_leaves_no_partial_table(tmp_path: Path) -> None:
    database = Database(tmp_path / "broken-migration.db")
    database.initialize()
    with pytest.raises(sqlite3.OperationalError):
        with database.transaction() as connection:
            connection.execute("CREATE TABLE partial_migration (id INTEGER)")
            connection.execute("THIS IS NOT VALID SQL")
    with database.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial_migration'"
        ).fetchone()
    assert exists is None
