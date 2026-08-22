"""Immutable contracts shared by capture sources and private strategy plugins."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Instrument:
    """Public instrument identity; selection belongs outside the infrastructure."""

    instrument_id: str
    venue: str
    market_id: str | None = None
    token_id: str | None = None
    asset: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.instrument_id.strip() or not self.venue.strip():
            raise ValueError("instrument_id and venue are required")
        object.__setattr__(self, "observed_at", utc(self.observed_at))


@dataclass(frozen=True)
class RawEvent:
    """One unmodeled public event with venue and local receipt clocks."""

    venue: str
    channel: str
    event_type: str
    instrument_id: str
    received_at: datetime
    raw_payload: dict[str, Any]
    source_version: str
    payload_hash: str | None = None
    exchange_at: datetime | None = None
    sequence_start: int | None = None
    sequence_end: int | None = None
    timestamp_trusted: bool = True

    def __post_init__(self) -> None:
        required = (self.venue, self.channel, self.event_type, self.instrument_id)
        if any(not item.strip() for item in required):
            raise ValueError("venue, channel, event_type, and instrument_id are required")
        object.__setattr__(self, "received_at", utc(self.received_at))
        if self.exchange_at is not None:
            object.__setattr__(self, "exchange_at", utc(self.exchange_at))
        digest = self.payload_hash or payload_sha256(self.raw_payload)
        if len(digest) != 64:
            raise ValueError("payload_hash must be SHA-256 hex")
        object.__setattr__(self, "payload_hash", digest.lower())


@dataclass(frozen=True)
class CaptureResult:
    session_id: str
    status: str
    persisted_events: int
    duplicate_events: int
    source_health: dict[str, object]
    started_at: datetime
    completed_at: datetime
