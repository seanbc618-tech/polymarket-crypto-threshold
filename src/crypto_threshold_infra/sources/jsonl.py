"""Deterministic JSONL source for smoke tests and offline integration."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from crypto_threshold_infra.models import RawEvent


class JsonlSource:
    def __init__(self, path: str | Path, *, name: str = "jsonl") -> None:
        self.path = Path(path).expanduser().resolve()
        self._name = name
        self._events: list[RawEvent] = []
        self._started = False

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self._events = [_event_from_mapping(row) for row in rows if row]
        self._started = True

    def replace_instruments(self, instruments: tuple[object, ...]) -> None:
        del instruments

    def drain(self, *, limit: int) -> tuple[RawEvent, ...]:
        if not self._started:
            return ()
        result = tuple(self._events[:limit])
        del self._events[:limit]
        return result

    def health(self) -> dict[str, object]:
        return {
            "status": "connected" if self._started else "disabled",
            "queued": len(self._events),
            "dropped": 0,
        }

    def stop(self) -> None:
        self._started = False


def create_source(config: dict[str, Any]) -> JsonlSource:
    path = config.get("path")
    if not path:
        raise ValueError("JSONL source config requires path")
    return JsonlSource(path, name=str(config.get("name") or "jsonl"))


def _event_from_mapping(value: object) -> RawEvent:
    if not isinstance(value, dict):
        raise ValueError("JSONL event must be an object")
    exchange_at = value.get("exchange_at")
    return RawEvent(
        venue=str(value["venue"]),
        channel=str(value["channel"]),
        event_type=str(value["event_type"]),
        instrument_id=str(value["instrument_id"]),
        received_at=datetime.fromisoformat(str(value["received_at"]).replace("Z", "+00:00")),
        exchange_at=(
            datetime.fromisoformat(str(exchange_at).replace("Z", "+00:00")) if exchange_at else None
        ),
        raw_payload=dict(value.get("raw_payload") or {}),
        source_version=str(value.get("source_version") or "jsonl-v1"),
        payload_hash=str(value["payload_hash"]) if value.get("payload_hash") else None,
        sequence_start=_optional_int(value.get("sequence_start")),
        sequence_end=_optional_int(value.get("sequence_end")),
        timestamp_trusted=bool(value.get("timestamp_trusted", True)),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(str(value))
