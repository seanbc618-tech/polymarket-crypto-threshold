"""Interface example only: records event counts and makes no market decision."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from crypto_threshold_infra.models import Instrument, RawEvent
from crypto_threshold_infra.plugin import PluginContext, StrategyRecord


class NoopStrategy:
    name = "noop-interface-example"

    def start(self, context: PluginContext) -> Iterable[StrategyRecord]:
        del context
        return ()

    def replace_instruments(
        self, instruments: tuple[Instrument, ...], *, at: datetime
    ) -> Iterable[StrategyRecord]:
        del instruments, at
        return ()

    def observe(self, events: tuple[RawEvent, ...]) -> Iterable[StrategyRecord]:
        del events
        return ()

    def tick(
        self, *, at: datetime, source_health: Mapping[str, object]
    ) -> Iterable[StrategyRecord]:
        del at, source_health
        return ()

    def finish(self, *, status: str, at: datetime) -> Iterable[StrategyRecord]:
        del status, at
        return ()


def create_strategy(config: dict[str, Any]) -> NoopStrategy:
    del config
    return NoopStrategy()
