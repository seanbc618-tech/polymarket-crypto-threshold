"""Continuity tests for Binance snapshot/diff tape construction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_threshold.domain.microstructure import L2Level, TradeAggressor
from crypto_threshold.domain.microstructure_capture import (
    RawMicrostructureEvent,
    RawMicrostructureKind,
)
from crypto_threshold.services.binance_tape_service import (
    BinanceTapeError,
    BinanceTapeService,
)
from crypto_threshold.storage.microstructure_store import MicrostructureStore


def _event(
    *,
    kind: RawMicrostructureKind,
    at: datetime,
    payload_hash: str,
    sequence_start: int,
    sequence_end: int,
) -> RawMicrostructureEvent:
    if kind is RawMicrostructureKind.SNAPSHOT:
        return RawMicrostructureEvent(
            symbol="BTCUSDT",
            kind=kind,
            exchange_at=at,
            received_at=at + timedelta(milliseconds=2),
            source="test",
            source_version="test-v1",
            payload_hash=payload_hash,
            raw_payload={"kind": "snapshot"},
            venue_sequence_start=sequence_start,
            venue_sequence_end=sequence_end,
            bids=(L2Level(price=Decimal("100"), quantity=Decimal("2")),),
            asks=(L2Level(price=Decimal("101"), quantity=Decimal("2")),),
        )
    if kind is RawMicrostructureKind.DEPTH:
        return RawMicrostructureEvent(
            symbol="BTCUSDT",
            kind=kind,
            exchange_at=at,
            received_at=at + timedelta(milliseconds=2),
            source="test",
            source_version="test-v1",
            payload_hash=payload_hash,
            raw_payload={"kind": "depth"},
            venue_sequence_start=sequence_start,
            venue_sequence_end=sequence_end,
            bids=(),
            asks=(),
        )
    return RawMicrostructureEvent(
        symbol="BTCUSDT",
        kind=kind,
        exchange_at=at,
        received_at=at + timedelta(milliseconds=2),
        source="test",
        source_version="test-v1",
        payload_hash=payload_hash,
        raw_payload={"kind": "trade"},
        venue_sequence_start=sequence_start,
        venue_sequence_end=sequence_end,
        price=Decimal("101"),
        quantity=Decimal("0.1"),
        aggressor=TradeAggressor.BUY,
    )


def _build_rows(tmp_path: Path, *, gap: bool = False) -> tuple:
    store = MicrostructureStore(tmp_path / "tape.db")
    store.initialize()
    session = "micro:test"
    at = datetime(2026, 7, 30, 5, 0, tzinfo=UTC)
    store.start_session(
        session_id=session,
        symbols=("BTCUSDT",),
        config_hash="a" * 64,
        started_at=at,
        source_version="test-v1",
    )
    snapshot = _event(
        kind=RawMicrostructureKind.SNAPSHOT,
        at=at,
        payload_hash="1" * 64,
        sequence_start=100,
        sequence_end=100,
    )
    depth = RawMicrostructureEvent(
        symbol="BTCUSDT",
        kind=RawMicrostructureKind.DEPTH,
        exchange_at=at + timedelta(milliseconds=10),
        received_at=at + timedelta(milliseconds=12),
        source="test",
        source_version="test-v1",
        payload_hash="2" * 64,
        raw_payload={"kind": "depth"},
        venue_sequence_start=103 if gap else 101,
        venue_sequence_end=103 if gap else 101,
        bids=(L2Level(price=Decimal("100"), quantity=Decimal("1")),),
        asks=(L2Level(price=Decimal("101"), quantity=Decimal("1")),),
    )
    trade = _event(
        kind=RawMicrostructureKind.TRADE,
        at=at + timedelta(milliseconds=11),
        payload_hash="3" * 64,
        sequence_start=1,
        sequence_end=1,
    )
    store.save_events(session, (snapshot, depth, trade))
    return store.latest_tape_rows(session_id=session, symbol="BTCUSDT")


def test_tape_builds_snapshot_bridge_and_orders_trade_before_depth(tmp_path: Path) -> None:
    rows = _build_rows(tmp_path)
    tape = BinanceTapeService().build(rows)

    assert tape.symbol == "BTCUSDT"
    assert tape.snapshot_update_id == 100
    assert tape.final_update_id == 101
    assert tape.events[0].kind.value == "snapshot"
    assert any(event.kind.value == "trade" for event in tape.events)
    assert tape.raw_event_ids == (1, 2, 3)


def test_tape_rejects_sequence_gap(tmp_path: Path) -> None:
    with pytest.raises(BinanceTapeError, match="bridge_gap"):
        BinanceTapeService().build(_build_rows(tmp_path, gap=True))
