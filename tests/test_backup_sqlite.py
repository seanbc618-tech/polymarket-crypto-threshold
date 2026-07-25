from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _load_backup_module():
    script = Path(__file__).parents[1] / "scripts" / "backup_sqlite.py"
    spec = importlib.util.spec_from_file_location("backup_sqlite", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_is_consistent_and_prunes_old_copies(tmp_path: Path) -> None:
    module = _load_backup_module()
    database = tmp_path / "evidence.db"
    output_dir = tmp_path / "backups"

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
    connection.execute("INSERT INTO evidence VALUES ('read-only')")
    connection.commit()
    connection.close()

    output_dir.mkdir()
    old_one = output_dir / "crypto-threshold-20000101T000000.000000Z.db"
    old_two = output_dir / "crypto-threshold-20000102T000000.000000Z.db"
    old_one.write_bytes(b"old")
    old_two.write_bytes(b"old")

    backup = module.backup_database(database, output_dir, retention=2)

    restored = sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True)
    try:
        assert restored.execute("SELECT value FROM evidence").fetchone() == (
            "read-only",
        )
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        restored.close()
    assert [path.name for path in sorted(output_dir.glob("*.db"))] == [
        old_two.name,
        backup.name,
    ]
    assert not list(output_dir.glob("*.partial*"))


def test_backup_rejects_missing_database_and_invalid_retention(
    tmp_path: Path,
) -> None:
    module = _load_backup_module()
    missing = tmp_path / "missing.db"

    try:
        module.backup_database(missing, tmp_path / "backups", retention=1)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing source database should be rejected")

    database = tmp_path / "evidence.db"
    sqlite3.connect(database).close()
    try:
        module.backup_database(database, tmp_path / "backups", retention=0)
    except ValueError:
        pass
    else:
        raise AssertionError("retention=0 should be rejected")
