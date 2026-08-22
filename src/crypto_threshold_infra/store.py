"""Raw-only, hash-addressed SQLite persistence with fixed UTC partitions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from crypto_threshold_infra.models import Instrument, RawEvent, utc

STORE_VERSION = "raw-capture-store-v1"
PARTITION_HOURS = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    source_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    session_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    market_id TEXT,
    token_id TEXT,
    asset TEXT,
    metadata_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (session_id, venue, instrument_id)
);
CREATE TABLE IF NOT EXISTS raw_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    channel TEXT NOT NULL,
    event_type TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    exchange_at TEXT,
    received_at TEXT NOT NULL,
    sequence_start INTEGER,
    sequence_end INTEGER,
    timestamp_trusted INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    UNIQUE (
        venue, channel, event_type, instrument_id, received_at, payload_hash
    )
);
CREATE INDEX IF NOT EXISTS idx_raw_events_received
    ON raw_events(received_at);
CREATE INDEX IF NOT EXISTS idx_raw_events_channel
    ON raw_events(venue, channel, received_at);
"""


class CaptureStore:
    """Writes only raw capture contracts; plugin outputs use a separate store."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.session_id: str | None = None
        self.started_at: datetime | None = None
        self._config_json: str | None = None
        self._config_hash: str | None = None
        self._touched: set[Path] = set()

    def start_session(self, *, config: dict[str, object], started_at: datetime) -> str:
        if self.session_id is not None:
            raise RuntimeError("capture session already started")
        self.root.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
        self.session_id = f"capture:{uuid4()}"
        self.started_at = utc(started_at)
        self._config_json = encoded
        self._config_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self._ensure_session(self.path_for(self.started_at))
        return self.session_id

    def register_instruments(self, instruments: Iterable[Instrument], *, at: datetime) -> int:
        rows = tuple(instruments)
        if not rows:
            return 0
        path = self.path_for(at)
        self._ensure_session(path)
        with _transaction(path) as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO instruments (
                    session_id, venue, instrument_id, market_id, token_id,
                    asset, metadata_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, venue, instrument_id) DO UPDATE SET
                    market_id=excluded.market_id,
                    token_id=excluded.token_id,
                    asset=excluded.asset,
                    metadata_json=excluded.metadata_json,
                    observed_at=excluded.observed_at
                """,
                (
                    (
                        self._session(),
                        item.venue,
                        item.instrument_id,
                        item.market_id,
                        item.token_id,
                        item.asset,
                        json.dumps(item.metadata, sort_keys=True, separators=(",", ":")),
                        item.observed_at.isoformat(),
                    )
                    for item in rows
                ),
            )
            return connection.total_changes - before

    def save_events(self, events: Iterable[RawEvent]) -> tuple[int, int]:
        grouped: dict[Path, list[RawEvent]] = {}
        for event in events:
            grouped.setdefault(self.path_for(event.received_at), []).append(event)
        inserted = 0
        attempted = 0
        for path, rows in sorted(grouped.items(), key=lambda item: str(item[0])):
            self._ensure_session(path)
            with _transaction(path) as connection:
                before = connection.total_changes
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO raw_events (
                        session_id, venue, channel, event_type, instrument_id,
                        exchange_at, received_at, sequence_start, sequence_end,
                        timestamp_trusted, source_version, payload_hash, raw_payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            self._session(),
                            event.venue,
                            event.channel,
                            event.event_type,
                            event.instrument_id,
                            _iso(event.exchange_at),
                            event.received_at.isoformat(),
                            event.sequence_start,
                            event.sequence_end,
                            int(event.timestamp_trusted),
                            event.source_version,
                            event.payload_hash,
                            json.dumps(
                                event.raw_payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                        for event in rows
                    ),
                )
                inserted += connection.total_changes - before
                attempted += len(rows)
        return inserted, attempted - inserted

    def finish_session(self, *, status: str, completed_at: datetime) -> None:
        session_id = self.session_id
        if session_id is None:
            return
        for path in sorted(self._touched):
            if not path.is_file():
                continue
            with _transaction(path) as connection:
                connection.execute(
                    """
                    UPDATE capture_sessions
                    SET status = ?, completed_at = ?
                    WHERE session_id = ?
                    """,
                    (status, utc(completed_at).isoformat(), session_id),
                )

    def path_for(self, at: datetime) -> Path:
        current = utc(at)
        start_hour = current.hour - current.hour % PARTITION_HOURS
        return self.root / f"raw-events-{current:%Y%m%d}T{start_hour:02d}.db"

    def _ensure_session(self, path: Path) -> None:
        if path in self._touched:
            return
        if self.started_at is None or self._config_json is None or self._config_hash is None:
            raise RuntimeError("capture session was not started")
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                INSERT OR IGNORE INTO capture_sessions (
                    session_id, started_at, status, config_json,
                    config_hash, source_version
                ) VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (
                    self._session(),
                    self.started_at.isoformat(),
                    self._config_json,
                    self._config_hash,
                    STORE_VERSION,
                ),
            )
        self._touched.add(path)

    def _session(self) -> str:
        if self.session_id is None:
            raise RuntimeError("capture session was not started")
        return self.session_id


def summarize_store(root: str | Path) -> dict[str, object]:
    directory = Path(root).expanduser().resolve()
    paths = sorted(directory.glob("raw-events-????????T??.db")) if directory.is_dir() else []
    venues: Counter[str] = Counter()
    events = 0
    instruments = 0
    first_received_at: str | None = None
    last_received_at: str | None = None
    errors: list[str] = []
    for path in paths:
        try:
            uri = f"file:{path}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                connection.execute("PRAGMA query_only=ON")
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity != ("ok",):
                    raise RuntimeError(f"integrity_check={integrity}")
                events += int(connection.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])
                instruments += int(
                    connection.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
                )
                for venue, count in connection.execute(
                    "SELECT venue, COUNT(*) FROM raw_events GROUP BY venue"
                ):
                    venues[str(venue)] += int(count)
                bounds = connection.execute(
                    "SELECT MIN(received_at), MAX(received_at) FROM raw_events"
                ).fetchone()
                if bounds[0] is not None:
                    first_received_at = min(
                        value for value in (first_received_at, str(bounds[0])) if value is not None
                    )
                if bounds[1] is not None:
                    last_received_at = max(
                        value for value in (last_received_at, str(bounds[1])) if value is not None
                    )
        except Exception as exc:
            errors.append(f"{path.name}:{type(exc).__name__}:{exc}")
    return {
        "directory": str(directory),
        "partitions": len(paths),
        "bytes": sum(path.stat().st_size for path in paths),
        "events": events,
        "instruments": instruments,
        "venues": dict(sorted(venues.items())),
        "first_received_at": first_received_at,
        "last_received_at": last_received_at,
        "errors": errors,
    }


def _transaction(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _iso(value: datetime | None) -> str | None:
    return None if value is None else utc(value).isoformat()
