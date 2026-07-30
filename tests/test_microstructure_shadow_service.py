"""End-to-end one-cycle test for the isolated public-data shadow."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_threshold.domain.microstructure import L2Level, TradeAggressor
from crypto_threshold.domain.microstructure_capture import (
    MicrostructureFeatureSample,
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
        purge_seconds=600,
        embargo_seconds=300,
        depth_levels=1,
        trade_lookback_seconds=5,
        event_batch_limit=100,
        integrity_sample_limit=102,
        stream_ready_timeout_seconds=1,
        frozen_model_version="cex-kline-chainlink-direction-v1+49093373ec3e",
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
    connection = store.connect()
    try:
        factor_row = connection.execute(
            "SELECT report_json FROM factor_screening_runs"
        ).fetchone()
    finally:
        connection.close()
    factor_report = json.loads(str(factor_row["report_json"]))
    assert (
        factor_report["spec"]["frozen_model_version"]
        == "cex-kline-chainlink-direction-v1+49093373ec3e"
    )

    for index in range(1, 102):
        sample_at = at + timedelta(seconds=index * 5)
        assert store.save_feature_sample(
            MicrostructureFeatureSample(
                sample_id=f"feature:integrity:{index}",
                session_id=session_id,
                symbol="BTCUSDT",
                as_of_exchange_at=sample_at,
                as_of_received_at=sample_at + timedelta(milliseconds=2),
                best_bid=Decimal("100"),
                best_ask=Decimal("101"),
                midpoint=Decimal("100.5"),
                spread=Decimal("1"),
                bid_depth=Decimal("2"),
                ask_depth=Decimal("2"),
                book_imbalance=Decimal("0"),
                microprice=Decimal("100.5"),
                vamp=Decimal("100.5"),
                aggressive_trade_imbalance=Decimal("0"),
                feed_latency_ms=Decimal("2"),
                spot_perpetual_basis_bps=Decimal("0"),
                btc_lead_correlation=None,
                source_event_ids=(1, 2),
                source_payload_hashes=("5" * 64,),
            )
        )
    reasons: list[str] = []
    assert service._run_integrity(session_id, reasons=reasons) == 1
    assert any("integrity_split_collecting" in reason for reason in reasons)
    connection = store.connect()
    try:
        row = connection.execute(
            "SELECT status, row_count FROM research_integrity_runs"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("collecting_split", 102)
