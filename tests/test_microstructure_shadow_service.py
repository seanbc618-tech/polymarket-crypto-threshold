"""End-to-end one-cycle test for the isolated public-data shadow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_threshold.domain.microstructure import L2Level, TradeAggressor
from crypto_threshold.domain.microstructure_capture import (
    PerpetualMark,
    RawMicrostructureEvent,
    RawMicrostructureKind,
)
from crypto_threshold.services.microstructure_shadow_service import (
    MicrostructureShadowConfig,
    MicrostructureShadowService,
)
from crypto_threshold.storage.microstructure_store import MicrostructureStore


class _Stream:
    def __init__(self, events: tuple[RawMicrostructureEvent, ...]) -> None:
        self.events = list(events)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def health(self) -> dict[str, object]:
        return {
            "status": "connected",
            "detail": {
                "dropped": 0,
                "generation": 1,
                "observed_symbols": ["BTCUSDT"],
            },
        }

    def drain(self, *, limit: int) -> tuple[RawMicrostructureEvent, ...]:
        result = tuple(self.events[:limit])
        del self.events[:limit]
        return result


class _Rest:
    def __init__(
        self,
        snapshot: RawMicrostructureEvent,
        mark: PerpetualMark,
    ) -> None:
        self.snapshot = snapshot
        self.mark = mark
        self.closed = False

    def depth_snapshot(self, symbol: str) -> RawMicrostructureEvent:
        assert symbol == "BTCUSDT"
        return self.snapshot

    def perpetual_mark(self, symbol: str) -> PerpetualMark:
        assert symbol == "BTCUSDT"
        return self.mark

    def close(self) -> None:
        self.closed = True


def test_one_cycle_persists_features_and_preregistered_factor_plan(tmp_path: Path) -> None:
    at = datetime(2026, 7, 30, 5, 0, tzinfo=UTC)
    snapshot = RawMicrostructureEvent(
        symbol="BTCUSDT",
        kind=RawMicrostructureKind.SNAPSHOT,
        exchange_at=at,
        received_at=at + timedelta(milliseconds=2),
        source="binance_spot_rest",
        source_version="snapshot-v1",
        payload_hash="1" * 64,
        raw_payload={"lastUpdateId": 100},
        venue_sequence_start=100,
        venue_sequence_end=100,
        bids=(L2Level(price=Decimal("100"), quantity=Decimal("2")),),
        asks=(L2Level(price=Decimal("101"), quantity=Decimal("2")),),
        timestamp_trusted=False,
    )
    depth = RawMicrostructureEvent(
        symbol="BTCUSDT",
        kind=RawMicrostructureKind.DEPTH,
        exchange_at=at + timedelta(milliseconds=10),
        received_at=at + timedelta(milliseconds=12),
        source="binance_spot_websocket",
        source_version="stream-v1",
        payload_hash="2" * 64,
        raw_payload={"u": 101},
        venue_sequence_start=101,
        venue_sequence_end=101,
        bids=(L2Level(price=Decimal("100"), quantity=Decimal("2")),),
        asks=(L2Level(price=Decimal("101"), quantity=Decimal("2")),),
    )
    trade = RawMicrostructureEvent(
        symbol="BTCUSDT",
        kind=RawMicrostructureKind.TRADE,
        exchange_at=at + timedelta(milliseconds=11),
        received_at=at + timedelta(milliseconds=13),
        source="binance_spot_websocket",
        source_version="stream-v1",
        payload_hash="3" * 64,
        raw_payload={"a": 1},
        venue_sequence_start=1,
        venue_sequence_end=1,
        price=Decimal("101"),
        quantity=Decimal("0.1"),
        aggressor=TradeAggressor.BUY,
    )
    mark = PerpetualMark(
        symbol="BTCUSDT",
        mark_price=Decimal("100.5"),
        index_price=Decimal("100"),
        funding_rate=None,
        exchange_at=at,
        received_at=at + timedelta(milliseconds=4),
        payload_hash="4" * 64,
        raw_payload={"markPrice": "100.5"},
    )
    store = MicrostructureStore(tmp_path / "shadow.db")
    store.initialize()
    stream = _Stream((depth, trade))
    rest = _Rest(snapshot, mark)
    config = MicrostructureShadowConfig(
        symbols=("BTCUSDT",),
        poll_seconds=0.01,
        snapshot_seconds=60,
        feature_seconds=5,
        integrity_seconds=300,
        depth_levels=1,
        trade_lookback_seconds=5,
        event_batch_limit=100,
        integrity_sample_limit=102,
        stream_ready_timeout_seconds=1,
        warmup_seconds=0,
    )
    service = MicrostructureShadowService(
        store=store,  # type: ignore[arg-type]
        stream=stream,  # type: ignore[arg-type]
        rest=rest,  # type: ignore[arg-type]
        config=config,
        clock=lambda: at + timedelta(seconds=1),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
    )

    session_id = service.run(once=True)
    summary = store.summary()
    assert session_id.startswith("micro:")
    assert stream.started is True
    assert stream.stopped is True
    assert rest.closed is True
    assert summary["events"] == 4
    assert summary["feature_samples"] == 1
    assert summary["factor_runs"] == 1
    assert summary["integrity_runs"] == 0
    assert summary["session"] is not None
    assert summary["session"]["status"] == "complete_with_rejections"
