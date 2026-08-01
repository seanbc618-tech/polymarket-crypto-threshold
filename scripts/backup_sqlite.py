#!/usr/bin/env python3
"""Create an atomic SQLite backup without mutating the source database."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup_database(
    database: Path,
    output_dir: Path,
    retention: int,
    *,
    skip_unchanged: bool = False,
) -> Path:
    """Back up one SQLite database and retain the newest requested copies."""
    source_path = database.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"database does not exist: {source_path}")
    if retention < 1:
        raise ValueError("retention must be at least 1")

    destination_dir = output_dir.expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing_backups = _list_backups(destination_dir)
    latest = existing_backups[0] if existing_backups else None
    if skip_unchanged and latest is not None and _source_is_older_than_backup(source_path, latest):
        _prune_backups(destination_dir, retention)
        return latest

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = destination_dir / f"crypto-threshold-{timestamp}.db"
    temporary = destination.with_suffix(".db.partial")
    temporary_sidecars = (
        Path(f"{temporary}-wal"),
        Path(f"{temporary}-shm"),
    )

    try:
        source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        try:
            source.execute("PRAGMA query_only = ON")
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            journal_mode = target.execute("PRAGMA journal_mode = DELETE").fetchone()
            if journal_mode != ("delete",):
                raise RuntimeError(f"backup journal mode normalization failed: {journal_mode!r}")
        finally:
            target.close()
            source.close()

        check = sqlite3.connect(f"{temporary.as_uri()}?mode=ro", uri=True)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
        if integrity != ("ok",):
            raise RuntimeError(f"backup integrity check failed: {integrity!r}")

        os.chmod(temporary, 0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(destination)
        _prune_backups(destination_dir, retention)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
        for sidecar in temporary_sidecars:
            sidecar.unlink(missing_ok=True)


def _prune_backups(output_dir: Path, retention: int) -> None:
    backups = _list_backups(output_dir)
    for stale in backups[retention:]:
        stale.unlink()


def _list_backups(output_dir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in output_dir.glob("crypto-threshold-*.db")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.name,
        reverse=True,
    )


def _source_is_older_than_backup(source_path: Path, backup: Path) -> bool:
    """Return true only when the SQLite source has no newer WAL-backed state."""
    source_mtime_ns = source_path.stat().st_mtime_ns
    wal_path = Path(f"{source_path}-wal")
    if wal_path.exists():
        wal_stat = wal_path.stat()
        if wal_stat.st_size > 0:
            return False
        source_mtime_ns = max(source_mtime_ns, wal_stat.st_mtime_ns)
    return backup.stat().st_mtime_ns >= source_mtime_ns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--retention", type=int, default=7)
    parser.add_argument(
        "--skip-unchanged",
        action="store_true",
        help="reuse the newest backup when the source and empty WAL are older",
    )
    args = parser.parse_args()

    destination = backup_database(
        args.database,
        args.output_dir,
        args.retention,
        skip_unchanged=args.skip_unchanged,
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
