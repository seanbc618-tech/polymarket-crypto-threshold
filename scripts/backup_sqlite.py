#!/usr/bin/env python3
"""Create an atomic SQLite backup without mutating the source database."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup_database(database: Path, output_dir: Path, retention: int) -> Path:
    """Back up one SQLite database and retain the newest requested copies."""
    source_path = database.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"database does not exist: {source_path}")
    if retention < 1:
        raise ValueError("retention must be at least 1")

    destination_dir = output_dir.expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                raise RuntimeError(
                    f"backup journal mode normalization failed: {journal_mode!r}"
                )
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
    backups = sorted(
        (
            path
            for path in output_dir.glob("crypto-threshold-*.db")
            if path.is_file()
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    for stale in backups[retention:]:
        stale.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--retention", type=int, default=7)
    args = parser.parse_args()

    destination = backup_database(args.database, args.output_dir, args.retention)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
