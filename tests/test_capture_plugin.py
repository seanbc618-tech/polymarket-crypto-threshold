from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from crypto_threshold_infra.capture import CaptureEngine, CaptureHealthError
from crypto_threshold_infra.catalog import InstrumentCatalog
from crypto_threshold_infra.models import Instrument, RawEvent
from crypto_threshold_infra.plugin import (
    AsyncPluginHost,
    PluginContext,
    PluginRecordStore,
    StrategyRecord,
)
from crypto_threshold_infra.store import CaptureStore

NOW = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


class Source:
    name = "public-test-source"

    def __init__(self, *, dropped: int = 0) -> None:
        self.events = [
            RawEvent(
                venue="venue",
                channel="public",
                event_type="update",
                instrument_id="i-1",
                received_at=NOW,
                exchange_at=NOW,
                raw_payload={"value": "1"},
                source_version="test-v1",
            )
        ]
        self.dropped = dropped

    def start(self) -> None:
        pass

    def replace_instruments(self, instruments: tuple[object, ...]) -> None:
        assert len(instruments) == 1

    def drain(self, *, limit: int) -> tuple[RawEvent, ...]:
        result = tuple(self.events[:limit])
        self.events.clear()
        return result

    def health(self) -> Mapping[str, object]:
        return {"status": "connected", "dropped": self.dropped}

    def stop(self) -> None:
        pass


class RecordingPlugin:
    name = "test-plugin"

    def start(self, context: PluginContext) -> Iterable[StrategyRecord]:
        del context
        return ()

    def replace_instruments(
        self, instruments: tuple[Instrument, ...], *, at: datetime
    ) -> Iterable[StrategyRecord]:
        del instruments, at
        return ()

    def observe(self, events: tuple[RawEvent, ...]) -> Iterable[StrategyRecord]:
        return (StrategyRecord("batch", events[-1].received_at, {"events": len(events)}),)

    def tick(
        self, *, at: datetime, source_health: Mapping[str, object]
    ) -> Iterable[StrategyRecord]:
        del at, source_health
        return ()

    def finish(self, *, status: str, at: datetime) -> Iterable[StrategyRecord]:
        del status, at
        return ()


class FailingPlugin(RecordingPlugin):
    name = "failing-test-plugin"

    def observe(self, events: tuple[RawEvent, ...]) -> Iterable[StrategyRecord]:
        del events
        raise RuntimeError("private plugin failure")


def _catalog(tmp_path: Path) -> InstrumentCatalog:
    catalog = InstrumentCatalog(tmp_path / "catalog.db")
    catalog.replace([Instrument("i-1", "venue", observed_at=NOW)])
    return catalog


def test_capture_persists_before_isolated_plugin_output(tmp_path: Path) -> None:
    plugin_path = tmp_path / "plugin.db"
    host = AsyncPluginHost(RecordingPlugin(), PluginRecordStore(plugin_path))
    engine = CaptureEngine(
        catalog=_catalog(tmp_path),
        store=CaptureStore(tmp_path / "raw"),
        sources=(Source(),),
        plugin_host=host,
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
        sleeper=lambda _: None,
    )
    result = engine.run(once=True)
    assert result.persisted_events == 1
    assert host.health()["failure_reason"] is None
    with sqlite3.connect(plugin_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plugin_records").fetchone() == (1,)
    raw_path = next((tmp_path / "raw").glob("*.db"))
    with sqlite3.connect(raw_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "plugin_records" not in tables


def test_source_drop_fails_closed_after_raw_persistence(tmp_path: Path) -> None:
    engine = CaptureEngine(
        catalog=_catalog(tmp_path),
        store=CaptureStore(tmp_path / "raw"),
        sources=(Source(dropped=1),),
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
        sleeper=lambda _: None,
    )
    with pytest.raises(CaptureHealthError) as failure:
        engine.run(once=True)
    assert failure.value.reasons == ("source_public-test-source_dropped:1",)


def test_private_plugin_failure_does_not_stop_raw_capture(tmp_path: Path) -> None:
    host = AsyncPluginHost(
        FailingPlugin(),
        PluginRecordStore(tmp_path / "plugin.db"),
        finish_timeout_seconds=1,
    )
    engine = CaptureEngine(
        catalog=_catalog(tmp_path),
        store=CaptureStore(tmp_path / "raw"),
        sources=(Source(),),
        plugin_host=host,
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
        sleeper=lambda _: None,
    )
    result = engine.run(once=True)
    assert result.status == "completed"
    assert result.persisted_events == 1
    assert host.health()["failure_reason"] == "plugin_exception:RuntimeError"
