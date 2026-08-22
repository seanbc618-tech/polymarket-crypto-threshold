from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from crypto_threshold_infra.models import Instrument, RawEvent
from crypto_threshold_infra.store import CaptureStore, summarize_store

NOW = datetime(2026, 8, 22, 3, 59, tzinfo=UTC)


def test_raw_store_partitions_deduplicates_and_has_no_plugin_tables(tmp_path) -> None:
    store = CaptureStore(tmp_path / "raw")
    store.start_session(config={"features": [], "signals": []}, started_at=NOW)
    store.register_instruments(
        [Instrument(instrument_id="i-1", venue="venue", observed_at=NOW)], at=NOW
    )
    first = _event(NOW, "one")
    second = _event(NOW + timedelta(minutes=2), "two")
    assert store.save_events((first, first, second)) == (2, 1)
    store.finish_session(status="completed", completed_at=second.received_at)

    paths = sorted((tmp_path / "raw").glob("*.db"))
    assert [path.name for path in paths] == [
        "raw-events-20260822T00.db",
        "raw-events-20260822T04.db",
    ]
    with sqlite3.connect(paths[0]) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "raw_events" in tables
    assert "plugin_records" not in tables
    summary = summarize_store(tmp_path / "raw")
    assert summary["events"] == 2
    assert summary["venues"] == {"venue": 2}
    assert summary["errors"] == []


def _event(at: datetime, value: str) -> RawEvent:
    return RawEvent(
        venue="venue",
        channel="public",
        event_type="update",
        instrument_id="i-1",
        exchange_at=at,
        received_at=at,
        raw_payload={"value": value},
        source_version="test-v1",
    )
