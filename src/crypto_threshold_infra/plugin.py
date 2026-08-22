"""Private strategy plugin contract and fail-closed asynchronous host."""

from __future__ import annotations

import importlib
import inspect
import json
import queue
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

from crypto_threshold_infra.models import Instrument, RawEvent, utc


@dataclass(frozen=True)
class PluginContext:
    capture_session_id: str
    plugin_session_id: str
    started_at: datetime
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyRecord:
    """Opaque shadow/research output; interpretation belongs to the plugin."""

    record_type: str
    observed_at: datetime
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.record_type.strip():
            raise ValueError("record_type is required")
        object.__setattr__(self, "observed_at", utc(self.observed_at))


@runtime_checkable
class StrategyPlugin(Protocol):
    """A strategy receives only already-persisted public observations."""

    @property
    def name(self) -> str: ...

    def start(self, context: PluginContext) -> Iterable[StrategyRecord]: ...

    def replace_instruments(
        self, instruments: tuple[Instrument, ...], *, at: datetime
    ) -> Iterable[StrategyRecord]: ...

    def observe(self, events: tuple[RawEvent, ...]) -> Iterable[StrategyRecord]: ...

    def tick(
        self, *, at: datetime, source_health: Mapping[str, object]
    ) -> Iterable[StrategyRecord]: ...

    def finish(self, *, status: str, at: datetime) -> Iterable[StrategyRecord]: ...


class PluginLoadError(RuntimeError):
    pass


def load_strategy_plugin(spec: str, config: Mapping[str, Any]) -> StrategyPlugin:
    """Load ``module:factory`` without placing private code in this repository."""

    module_name, separator, factory_name = spec.partition(":")
    if not separator or not module_name or not factory_name:
        raise PluginLoadError("strategy plugin must use module:factory")
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name, None)
    if factory is None or not callable(factory):
        raise PluginLoadError(f"strategy factory not callable: {spec}")
    plugin = factory(dict(config))
    if not isinstance(plugin, StrategyPlugin):
        missing = _missing_plugin_members(plugin)
        raise PluginLoadError(f"invalid strategy plugin; missing={','.join(missing)}")
    if not plugin.name.strip():
        raise PluginLoadError("strategy plugin name is empty")
    return plugin


def strategy_module_path(spec: str) -> Path | None:
    module_name = spec.partition(":")[0]
    module: ModuleType = importlib.import_module(module_name)
    source = inspect.getsourcefile(module)
    return Path(source).resolve() if source else None


class PluginRecordStore:
    """Separate store so plugin outputs can never contaminate raw partitions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.session_id: str | None = None

    def start(self, *, plugin_name: str, capture_session_id: str, at: datetime) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = f"plugin:{uuid4()}"
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plugin_sessions (
                    session_id TEXT PRIMARY KEY,
                    plugin_name TEXT NOT NULL,
                    capture_session_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plugin_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_plugin_records_time
                    ON plugin_records(observed_at);
                """
            )
            connection.execute(
                """
                INSERT INTO plugin_sessions (
                    session_id, plugin_name, capture_session_id, started_at, status
                ) VALUES (?, ?, ?, ?, 'running')
                """,
                (self.session_id, plugin_name, capture_session_id, utc(at).isoformat()),
            )
        return self.session_id

    def save(self, records: Iterable[StrategyRecord]) -> int:
        rows = tuple(records)
        if not rows:
            return 0
        if self.session_id is None:
            raise RuntimeError("plugin store was not started")
        with sqlite3.connect(self.path) as connection:
            connection.executemany(
                """
                INSERT INTO plugin_records (
                    session_id, record_type, observed_at, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        self.session_id,
                        record.record_type,
                        record.observed_at.isoformat(),
                        json.dumps(record.payload, sort_keys=True, separators=(",", ":")),
                    )
                    for record in rows
                ),
            )
        return len(rows)

    def finish(self, *, status: str, at: datetime) -> None:
        if self.session_id is None:
            return
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE plugin_sessions SET status = ?, completed_at = ?
                WHERE session_id = ?
                """,
                (status, utc(at).isoformat(), self.session_id),
            )


@dataclass(frozen=True)
class _Command:
    kind: str
    payload: object
    event_count: int = 0


class AsyncPluginHost:
    """Run private strategy work off the raw persistence thread."""

    def __init__(
        self,
        plugin: StrategyPlugin,
        store: PluginRecordStore,
        *,
        config: Mapping[str, Any] | None = None,
        max_commands: int = 64,
        max_events: int = 50_000,
        finish_timeout_seconds: float = 30.0,
    ) -> None:
        if max_commands < 2 or max_events < 1 or finish_timeout_seconds <= 0:
            raise ValueError("invalid async plugin limits")
        self.plugin = plugin
        self.store = store
        self.config = dict(config or {})
        self.max_events = max_events
        self.finish_timeout_seconds = finish_timeout_seconds
        self._commands: queue.Queue[_Command] = queue.Queue(maxsize=max_commands)
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._queued_events = 0
        self._failure_reason: str | None = None
        self._finish_requested = False
        self._finished = threading.Event()
        self._records = 0

    def start(self, *, at: datetime, capture_session_id: str) -> None:
        with self._lock:
            if self._worker is not None:
                raise RuntimeError("plugin host already started")
            plugin_session_id = self.store.start(
                plugin_name=self.plugin.name,
                capture_session_id=capture_session_id,
                at=at,
            )
            context = PluginContext(
                capture_session_id=capture_session_id,
                plugin_session_id=plugin_session_id,
                started_at=utc(at),
                config=self.config,
            )
            self._worker = threading.Thread(
                target=self._run,
                name="private-strategy-plugin",
                daemon=True,
            )
            self._worker.start()
        self._enqueue(_Command("start", context))

    def replace_instruments(self, instruments: tuple[Instrument, ...], *, at: datetime) -> None:
        self._enqueue(_Command("replace", (instruments, utc(at))))

    def observe(self, events: tuple[RawEvent, ...]) -> None:
        if events:
            self._enqueue(_Command("observe", events, event_count=len(events)))

    def tick(self, *, at: datetime, source_health: Mapping[str, object]) -> None:
        self._enqueue(_Command("tick", (utc(at), dict(source_health))))

    def finish(self, *, status: str, at: datetime) -> None:
        with self._lock:
            if self._finish_requested:
                worker = self._worker
                enqueue_finish = False
            else:
                self._finish_requested = True
                worker = self._worker
                enqueue_finish = True
        if worker is None:
            return
        deadline = time.monotonic() + self.finish_timeout_seconds
        if enqueue_finish and not self._finished.is_set():
            command = _Command("finish", (status, utc(at)))
            while not self._finished.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._mark_failed("plugin_finish_timeout")
                    break
                try:
                    self._commands.put(command, timeout=min(0.1, remaining))
                    break
                except queue.Full:
                    continue
        self._finished.wait(timeout=max(0.0, deadline - time.monotonic()))
        if not self._finished.is_set():
            self._mark_failed("plugin_finish_timeout")

    def health(self) -> dict[str, object]:
        with self._lock:
            return {
                "plugin": self.plugin.name,
                "queued_commands": self._commands.qsize(),
                "queued_events": self._queued_events,
                "max_events": self.max_events,
                "failure_reason": self._failure_reason,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
                "finished": self._finished.is_set(),
                "records": self._records,
            }

    def _enqueue(self, command: _Command) -> None:
        with self._lock:
            self._enqueue_unlocked(command)

    def _enqueue_unlocked(self, command: _Command) -> None:
        if self._failure_reason and command.kind != "finish":
            return
        if self._finish_requested and command.kind != "finish":
            return
        if self._queued_events + command.event_count > self.max_events:
            self._mark_failed_unlocked("plugin_event_backlog")
            return
        try:
            self._commands.put_nowait(command)
            self._queued_events += command.event_count
        except queue.Full:
            self._mark_failed_unlocked("plugin_command_backlog")

    def _run(self) -> None:
        status = "failed"
        try:
            while True:
                command = self._commands.get()
                try:
                    records: Iterable[StrategyRecord]
                    if command.kind == "start":
                        context = cast(PluginContext, command.payload)
                        records = self.plugin.start(context)
                    elif command.kind == "replace":
                        instruments, at = cast(
                            tuple[tuple[Instrument, ...], datetime], command.payload
                        )
                        records = self.plugin.replace_instruments(instruments, at=at)
                    elif command.kind == "observe":
                        events = cast(tuple[RawEvent, ...], command.payload)
                        records = self.plugin.observe(events)
                    elif command.kind == "tick":
                        at, health = cast(tuple[datetime, Mapping[str, object]], command.payload)
                        records = self.plugin.tick(at=at, source_health=health)
                    elif command.kind == "finish":
                        status, at = cast(tuple[str, datetime], command.payload)
                        records = self.plugin.finish(status=status, at=at)
                        self._save(records)
                        break
                    else:
                        raise RuntimeError(f"unknown plugin command: {command.kind}")
                    self._save(records)
                finally:
                    with self._lock:
                        self._queued_events -= command.event_count
                    self._commands.task_done()
        except Exception as exc:
            self._mark_failed(f"plugin_exception:{type(exc).__name__}")
            status = "failed"
        finally:
            self.store.finish(status=status, at=datetime.now(UTC))
            self._finished.set()

    def _save(self, records: Iterable[StrategyRecord] | None) -> None:
        if records is None:
            return
        saved = self.store.save(tuple(records))
        with self._lock:
            self._records += saved

    def _mark_failed(self, reason: str) -> None:
        with self._lock:
            self._mark_failed_unlocked(reason)

    def _mark_failed_unlocked(self, reason: str) -> None:
        if self._failure_reason is None:
            self._failure_reason = reason


def _missing_plugin_members(value: object) -> list[str]:
    names = ("name", "start", "replace_instruments", "observe", "tick", "finish")
    return [name for name in names if not hasattr(value, name)]
