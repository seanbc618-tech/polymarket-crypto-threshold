"""Persist-first orchestration for public sources and an optional private plugin."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from crypto_threshold_infra.catalog import InstrumentCatalog
from crypto_threshold_infra.models import CaptureResult, RawEvent, utc
from crypto_threshold_infra.plugin import AsyncPluginHost
from crypto_threshold_infra.store import CaptureStore


@runtime_checkable
class EventSource(Protocol):
    """Public source adapter loaded as ``module:factory``."""

    @property
    def name(self) -> str: ...

    def start(self) -> None: ...

    def replace_instruments(self, instruments: tuple[object, ...]) -> None: ...

    def drain(self, *, limit: int) -> tuple[RawEvent, ...]: ...

    def health(self) -> Mapping[str, object]: ...

    def stop(self) -> None: ...


class SourceLoadError(RuntimeError):
    pass


class CaptureHealthError(RuntimeError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__(";".join(reasons))


def load_event_source(spec: str, config: Mapping[str, Any]) -> EventSource:
    module_name, separator, factory_name = spec.partition(":")
    if not separator or not module_name or not factory_name:
        raise SourceLoadError("source must use module:factory")
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name, None)
    if factory is None or not callable(factory):
        raise SourceLoadError(f"source factory not callable: {spec}")
    source = factory(dict(config))
    if not isinstance(source, EventSource):
        raise SourceLoadError(f"invalid source adapter: {spec}")
    if not source.name.strip():
        raise SourceLoadError("source name is empty")
    return source


class CaptureEngine:
    """A strategy-neutral recorder; derived work runs only after durable save."""

    def __init__(
        self,
        *,
        catalog: InstrumentCatalog,
        store: CaptureStore,
        sources: tuple[EventSource, ...],
        plugin_host: AsyncPluginHost | None = None,
        poll_seconds: float = 0.5,
        batch_limit: int = 10_000,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not sources or poll_seconds <= 0 or batch_limit < 1:
            raise ValueError("invalid capture configuration")
        names = [source.name for source in sources]
        if len(names) != len(set(names)):
            raise ValueError("source names must be unique")
        self.catalog = catalog
        self.store = store
        self.sources = sources
        self.plugin_host = plugin_host
        self.poll_seconds = poll_seconds
        self.batch_limit = batch_limit
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep

    def run(
        self,
        *,
        once: bool = False,
        duration_seconds: float | None = None,
    ) -> CaptureResult:
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        started_at = utc(self.clock())
        instruments = self.catalog.list_enabled()
        session_id = self.store.start_session(
            config={
                "source_names": [source.name for source in self.sources],
                "poll_seconds": self.poll_seconds,
                "batch_limit": self.batch_limit,
                "plugin_attached": self.plugin_host is not None,
                "features": [],
                "signals": [],
            },
            started_at=started_at,
        )
        self.store.register_instruments(instruments, at=started_at)
        deadline = self.monotonic() + duration_seconds if duration_seconds is not None else None
        persisted = 0
        duplicates = 0
        status = "running"
        source_health: dict[str, object] = {}
        started_sources: list[EventSource] = []
        try:
            if self.plugin_host is not None:
                self.plugin_host.start(at=started_at, capture_session_id=session_id)
                self.plugin_host.replace_instruments(instruments, at=started_at)
            for source in self.sources:
                source.replace_instruments(tuple(instruments))
                source.start()
                started_sources.append(source)
            while True:
                events: list[RawEvent] = []
                remaining = self.batch_limit
                for source in self.sources:
                    if remaining <= 0:
                        break
                    drained = source.drain(limit=remaining)
                    events.extend(drained)
                    remaining -= len(drained)
                if events:
                    inserted, ignored = self.store.save_events(events)
                    persisted += inserted
                    duplicates += ignored
                    if self.plugin_host is not None:
                        self.plugin_host.observe(tuple(events))
                source_health = {source.name: dict(source.health()) for source in self.sources}
                self._assert_healthy(source_health)
                if self.plugin_host is not None:
                    self.plugin_host.tick(at=utc(self.clock()), source_health=source_health)
                now_mono = self.monotonic()
                if once or (deadline is not None and now_mono >= deadline):
                    status = "completed"
                    break
                self.sleeper(self.poll_seconds)
        except KeyboardInterrupt:
            status = "stopped"
        except Exception:
            status = "failed"
            raise
        finally:
            for source in reversed(started_sources):
                source.stop()
            completed_at = utc(self.clock())
            self.store.finish_session(status=status, completed_at=completed_at)
            if self.plugin_host is not None:
                plugin_status = "failed" if self.plugin_host.health()["failure_reason"] else status
                self.plugin_host.finish(status=str(plugin_status), at=completed_at)
        return CaptureResult(
            session_id=session_id,
            status=status,
            persisted_events=persisted,
            duplicate_events=duplicates,
            source_health=source_health,
            started_at=started_at,
            completed_at=utc(self.clock()),
        )

    @staticmethod
    def _assert_healthy(source_health: Mapping[str, object]) -> None:
        reasons: list[str] = []
        for name, raw in source_health.items():
            detail = raw if isinstance(raw, Mapping) else {}
            status = str(detail.get("status") or "unknown").lower()
            dropped = int(detail.get("dropped") or 0)
            if status in {"failed", "overflow"}:
                reasons.append(f"source_{name}_{status}")
            if dropped > 0:
                reasons.append(f"source_{name}_dropped:{dropped}")
        if reasons:
            raise CaptureHealthError(tuple(reasons))
