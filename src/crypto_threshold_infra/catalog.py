"""Small SQLite instrument catalog with explicit import and read contracts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crypto_threshold_infra.models import Instrument

SCHEMA = """
CREATE TABLE IF NOT EXISTS instrument_catalog (
    instrument_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    market_id TEXT,
    token_id TEXT,
    asset TEXT,
    metadata_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (venue, instrument_id)
);
"""


class InstrumentCatalog:
    """Catalog boundary used by the raw recorder and optional strategy plugin."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(SCHEMA)

    def replace(self, instruments: Iterable[Instrument]) -> int:
        rows = tuple(instruments)
        self.initialize()
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE instrument_catalog SET enabled = 0")
            connection.executemany(
                """
                INSERT INTO instrument_catalog (
                    instrument_id, venue, market_id, token_id, asset,
                    metadata_json, observed_at, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(venue, instrument_id) DO UPDATE SET
                    market_id=excluded.market_id,
                    token_id=excluded.token_id,
                    asset=excluded.asset,
                    metadata_json=excluded.metadata_json,
                    observed_at=excluded.observed_at,
                    enabled=1
                """,
                (
                    (
                        row.instrument_id,
                        row.venue,
                        row.market_id,
                        row.token_id,
                        row.asset,
                        json.dumps(row.metadata, sort_keys=True, separators=(",", ":")),
                        row.observed_at.isoformat(),
                    )
                    for row in rows
                ),
            )
        return len(rows)

    def list_enabled(self) -> tuple[Instrument, ...]:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        uri = f"file:{self.path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                """
                SELECT instrument_id, venue, market_id, token_id, asset,
                       metadata_json, observed_at
                FROM instrument_catalog
                WHERE enabled = 1
                ORDER BY venue, instrument_id
                """
            ).fetchall()
        return tuple(
            Instrument(
                instrument_id=str(row[0]),
                venue=str(row[1]),
                market_id=row[2],
                token_id=row[3],
                asset=row[4],
                metadata=json.loads(row[5]),
                observed_at=datetime.fromisoformat(row[6]).astimezone(UTC),
            )
            for row in rows
        )


def instruments_from_json(path: str | Path) -> tuple[Instrument, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("catalog JSON must be an array")
    result: list[Instrument] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each catalog item must be an object")
        observed = item.get("observed_at")
        result.append(
            Instrument(
                instrument_id=str(item["instrument_id"]),
                venue=str(item["venue"]),
                market_id=_optional_text(item.get("market_id")),
                token_id=_optional_text(item.get("token_id")),
                asset=_optional_text(item.get("asset")),
                metadata=_mapping(item.get("metadata")),
                observed_at=(
                    datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
                    if observed
                    else datetime.now(UTC)
                ),
            )
        )
    return tuple(result)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
