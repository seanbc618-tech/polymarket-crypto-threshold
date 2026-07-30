"""Isolated SQLite store for high-volume public microstructure shadow evidence."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from sqlite3 import Connection, Row, connect
from typing import Any, cast

from crypto_threshold.domain.microstructure_capture import (
    MicrostructureFeatureSample,
    PerpetualMark,
    RawMicrostructureEvent,
    RawMicrostructureKind,
)

MICROSTRUCTURE_SCHEMA_VERSION = 1

MICROSTRUCTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_sessions (
    session_id TEXT PRIMARY KEY,
    symbols_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    source_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_microstructure_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES capture_sessions(session_id),
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    exchange_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    venue_sequence_start INTEGER,
    venue_sequence_end INTEGER,
    timestamp_trusted INTEGER NOT NULL,
    normalized_json TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, symbol, kind, payload_hash, received_at)
);

CREATE TABLE IF NOT EXISTS microstructure_feature_samples (
    sample_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES capture_sessions(session_id),
    symbol TEXT NOT NULL,
    as_of_exchange_at TEXT NOT NULL,
    as_of_received_at TEXT NOT NULL,
    best_bid TEXT NOT NULL,
    best_ask TEXT NOT NULL,
    midpoint TEXT NOT NULL,
    spread TEXT NOT NULL,
    bid_depth TEXT NOT NULL,
    ask_depth TEXT NOT NULL,
    book_imbalance TEXT NOT NULL,
    microprice TEXT NOT NULL,
    vamp TEXT NOT NULL,
    aggressive_trade_imbalance TEXT NOT NULL,
    feed_latency_ms TEXT NOT NULL,
    spot_perpetual_basis_bps TEXT,
    btc_lead_correlation TEXT,
    source_event_ids_json TEXT NOT NULL,
    source_payload_hashes_json TEXT NOT NULL,
    source_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, symbol, as_of_exchange_at, source_version)
);

CREATE TABLE IF NOT EXISTS research_integrity_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES capture_sessions(session_id),
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS factor_screening_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES capture_sessions(session_id),
    experiment_id TEXT NOT NULL,
    status TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_micro_raw_symbol_id
ON raw_microstructure_events(symbol, event_id);

CREATE INDEX IF NOT EXISTS idx_micro_raw_session_kind
ON raw_microstructure_events(session_id, kind, event_id);

CREATE INDEX IF NOT EXISTS idx_micro_features_symbol_time
ON microstructure_feature_samples(symbol, as_of_exchange_at);
"""


class MicrostructureStore:
    """Small transaction owner kept separate from Daily and Up/Down databases."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path).expanduser().resolve()
        self.read_only = read_only

    def connect(self) -> Connection:
        if self.read_only:
            if not self.path.is_file():
                raise FileNotFoundError(f"database does not exist: {self.path}")
            connection = connect(
                f"{self.path.as_uri()}?mode=ro",
                timeout=30,
                uri=True,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = connect(self.path, timeout=30)
        connection.row_factory = Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def initialize(self) -> None:
        if self.read_only:
            raise RuntimeError("cannot initialize a read-only microstructure store")
        now = datetime.now(UTC).isoformat()
        with self.transaction() as connection:
            connection.executescript(MICROSTRUCTURE_SCHEMA)
            connection.execute(
                """
                INSERT INTO schema_meta (id, version, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (MICROSTRUCTURE_SCHEMA_VERSION, now),
            )

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        if self.read_only:
            raise RuntimeError("cannot write a read-only microstructure store")
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start_session(
        self,
        *,
        session_id: str,
        symbols: tuple[str, ...],
        config_hash: str,
        started_at: datetime,
        source_version: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO capture_sessions (
                    session_id, symbols_json, config_hash, status, reasons_json,
                    started_at, source_version
                ) VALUES (?, ?, ?, 'running', '[]', ?, ?)
                """,
                (
                    session_id,
                    _json(symbols),
                    config_hash,
                    _time(started_at),
                    source_version,
                ),
            )

    def finish_session(
        self,
        session_id: str,
        *,
        status: str,
        reasons: tuple[str, ...],
        completed_at: datetime,
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE capture_sessions
                SET status=?, reasons_json=?, completed_at=?
                WHERE session_id=?
                """,
                (status, _json(reasons), _time(completed_at), session_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("microstructure session was not found")

    def save_events(
        self,
        session_id: str,
        events: Iterable[RawMicrostructureEvent],
    ) -> tuple[int, ...]:
        identifiers: list[int] = []
        with self.transaction() as connection:
            for event in events:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO raw_microstructure_events (
                        session_id, symbol, kind, exchange_at, received_at,
                        source, source_version, payload_hash,
                        venue_sequence_start, venue_sequence_end,
                        timestamp_trusted, normalized_json, raw_payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        event.symbol,
                        event.kind.value,
                        _time(event.exchange_at),
                        _time(event.received_at),
                        event.source,
                        event.source_version,
                        event.payload_hash,
                        event.venue_sequence_start,
                        event.venue_sequence_end,
                        int(event.timestamp_trusted),
                        _json(
                            {
                                key: value
                                for key, value in asdict(event).items()
                                if key != "raw_payload"
                            }
                        ),
                        _json(event.raw_payload),
                    ),
                )
                if cursor.rowcount == 1:
                    if cursor.lastrowid is None:
                        raise RuntimeError("sqlite did not return inserted event id")
                    identifiers.append(cursor.lastrowid)
        return tuple(identifiers)

    def save_perpetual_marks(
        self,
        session_id: str,
        marks: Iterable[PerpetualMark],
    ) -> tuple[int, ...]:
        events = tuple(
            RawMicrostructureEvent(
                symbol=mark.symbol,
                kind=RawMicrostructureKind.PERPETUAL_MARK,
                exchange_at=mark.exchange_at,
                received_at=mark.received_at,
                source="binance_usdm_rest",
                source_version=mark.source_version,
                payload_hash=mark.payload_hash,
                raw_payload=mark.raw_payload,
                price=mark.mark_price,
                quantity=mark.index_price,
            )
            for mark in marks
        )
        return self.save_events(session_id, events)

    def latest_tape_rows(
        self,
        *,
        session_id: str,
        symbol: str,
        max_events: int = 50_000,
    ) -> tuple[Row, ...]:
        with closing(self.connect()) as connection:
            snapshot = connection.execute(
                """
                SELECT event_id
                FROM raw_microstructure_events
                WHERE session_id=? AND symbol=? AND kind='snapshot'
                ORDER BY event_id DESC
                LIMIT 1
                """,
                (session_id, symbol),
            ).fetchone()
            if snapshot is None:
                return ()
            rows = connection.execute(
                """
                SELECT *
                FROM raw_microstructure_events
                WHERE session_id=? AND symbol=? AND event_id>=?
                  AND kind IN ('snapshot', 'depth', 'trade')
                ORDER BY event_id
                LIMIT ?
                """,
                (session_id, symbol, int(snapshot["event_id"]), max_events + 1),
            ).fetchall()
            if len(rows) > max_events:
                raise ValueError("microstructure_tape_event_limit_exceeded")
        return tuple(rows)

    def latest_mark_row(
        self,
        *,
        session_id: str,
        symbol: str,
    ) -> Row | None:
        with closing(self.connect()) as connection:
            return connection.execute(
                """
                SELECT *
                FROM raw_microstructure_events
                WHERE session_id=? AND symbol=? AND kind='perpetual_mark'
                ORDER BY event_id DESC
                LIMIT 1
                """,
                (session_id, symbol),
            ).fetchone()

    def save_feature_sample(self, sample: MicrostructureFeatureSample) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO microstructure_feature_samples (
                    sample_id, session_id, symbol, as_of_exchange_at,
                    as_of_received_at, best_bid, best_ask, midpoint, spread,
                    bid_depth, ask_depth, book_imbalance, microprice, vamp,
                    aggressive_trade_imbalance, feed_latency_ms,
                    spot_perpetual_basis_bps, btc_lead_correlation,
                    source_event_ids_json, source_payload_hashes_json,
                    source_version
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    sample.sample_id,
                    sample.session_id,
                    sample.symbol,
                    _time(sample.as_of_exchange_at),
                    _time(sample.as_of_received_at),
                    str(sample.best_bid),
                    str(sample.best_ask),
                    str(sample.midpoint),
                    str(sample.spread),
                    str(sample.bid_depth),
                    str(sample.ask_depth),
                    str(sample.book_imbalance),
                    str(sample.microprice),
                    str(sample.vamp),
                    str(sample.aggressive_trade_imbalance),
                    str(sample.feed_latency_ms),
                    _optional_decimal(sample.spot_perpetual_basis_bps),
                    _optional_decimal(sample.btc_lead_correlation),
                    _json(sample.source_event_ids),
                    _json(sample.source_payload_hashes),
                    sample.source_version,
                ),
            )
        return cursor.rowcount == 1

    def recent_feature_rows(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> tuple[Row, ...]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM microstructure_feature_samples
                WHERE session_id=?
                ORDER BY as_of_exchange_at DESC, symbol
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return tuple(reversed(rows))

    def save_integrity_run(
        self,
        *,
        run_id: str,
        session_id: str,
        status: str,
        row_count: int,
        manifest_hash: str,
        report: object,
        created_at: datetime,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO research_integrity_runs (
                    run_id, session_id, status, row_count, manifest_hash,
                    report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    status,
                    row_count,
                    manifest_hash,
                    _json(report),
                    _time(created_at),
                ),
            )

    def save_factor_run(
        self,
        *,
        run_id: str,
        session_id: str,
        experiment_id: str,
        status: str,
        spec_hash: str,
        report: object,
        created_at: datetime,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO factor_screening_runs (
                    run_id, session_id, experiment_id, status, spec_hash,
                    report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    experiment_id,
                    status,
                    spec_hash,
                    _json(report),
                    _time(created_at),
                ),
            )

    def summary(self) -> dict[str, object]:
        with closing(self.connect()) as connection:
            session = connection.execute(
                """
                SELECT *
                FROM capture_sessions
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS events,
                    COUNT(DISTINCT symbol) AS symbols,
                    SUM(kind='snapshot') AS snapshots,
                    SUM(kind='depth') AS depth_events,
                    SUM(kind='trade') AS trades,
                    SUM(kind='perpetual_mark') AS marks
                FROM raw_microstructure_events
                """
            ).fetchone()
            feature_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM microstructure_feature_samples"
                ).fetchone()[0]
            )
            integrity_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM research_integrity_runs"
                ).fetchone()[0]
            )
            factor_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM factor_screening_runs"
                ).fetchone()[0]
            )
        return {
            "schema_version": MICROSTRUCTURE_SCHEMA_VERSION,
            "session": dict(session) if session is not None else None,
            "events": int(counts["events"] or 0),
            "symbols": int(counts["symbols"] or 0),
            "snapshots": int(counts["snapshots"] or 0),
            "depth_events": int(counts["depth_events"] or 0),
            "trades": int(counts["trades"] or 0),
            "marks": int(counts["marks"] or 0),
            "feature_samples": feature_count,
            "integrity_runs": integrity_count,
            "factor_runs": factor_count,
        }


def _optional_decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical(value: object) -> Any:
    if isinstance(value, datetime):
        return _time(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _canonical(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(item) for item in value]
    return value
