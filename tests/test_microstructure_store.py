"""Persistence and read-only contract tests for the isolated microstructure DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_threshold.domain.microstructure import L2Level, TradeAggressor
from crypto_threshold.domain.microstructure_capture import (
    MicrostructureFeatureSample,
    PerpetualMark,
    RawMicrostructureEvent,
    RawMicrostructureKind,
)
from crypto_threshold.storage.microstructure_store import MicrostructureStore


def test_store_persists_raw_feature_and_gate_rows_and_read_only_is_query_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "micro.db"
    store = MicrostructureStore(path)
    store.initialize()
    at = datetime(2026, 7, 30, 5, 0, tzinfo=UTC)
    session = "micro:test"
    store.start_session(
        session_id=session,
        symbols=("BTCUSDT",),
        config_hash="a" * 64,
        started_at=at,
        source_version="test-v1",
    )
    snapshot = RawMicrostructureEvent(
        symbol="BTCUSDT",
        kind=RawMicrostructureKind.SNAPSHOT,
        exchange_at=at,
        received_at=at + timedelta(milliseconds=2),
        source="test",
        source_version="test-v1",
        payload_hash="b" * 64,
        raw_payload={"e": "snapshot"},
        venue_sequence_start=100,
        venue_sequence_end=100,
        bids=(L2Level(price=Decimal("100"), quantity=Decimal("2")),),
        asks=(L2Level(price=Decimal("101"), quantity=Decimal("2")),),
    )
    trade = RawMicrostructureEvent(
        symbol="BTCUSDT",
        kind=RawMicrostructureKind.TRADE,
        exchange_at=at + timedelta(milliseconds=1),
        received_at=at + timedelta(milliseconds=3),
        source="test",
        source_version="test-v1",
        payload_hash="c" * 64,
        raw_payload={"e": "aggTrade"},
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
        funding_rate=Decimal("0.0001"),
        exchange_at=at,
        received_at=at + timedelta(milliseconds=4),
        payload_hash="d" * 64,
        raw_payload={"markPrice": "100.5"},
    )
    assert len(store.save_events(session, (snapshot, trade))) == 2
    assert len(store.save_perpetual_marks(session, (mark,))) == 1
    with pytest.raises(ValueError, match="tape_event_limit"):
        store.latest_tape_rows(
            session_id=session,
            symbol="BTCUSDT",
            max_events=1,
        )

    sample = MicrostructureFeatureSample(
        sample_id="feature:test",
        session_id=session,
        symbol="BTCUSDT",
        as_of_exchange_at=at + timedelta(milliseconds=1),
        as_of_received_at=at + timedelta(milliseconds=3),
        best_bid=Decimal("100"),
        best_ask=Decimal("101"),
        midpoint=Decimal("100.5"),
        spread=Decimal("1"),
        bid_depth=Decimal("2"),
        ask_depth=Decimal("2"),
        book_imbalance=Decimal("0"),
        microprice=Decimal("100.5"),
        vamp=Decimal("100.5"),
        aggressive_trade_imbalance=Decimal("1"),
        feed_latency_ms=Decimal("2"),
        spot_perpetual_basis_bps=Decimal("0"),
        btc_lead_correlation=None,
        source_event_ids=(1, 2),
        source_payload_hashes=("b" * 64, "c" * 64),
    )
    assert store.save_feature_sample(sample) is True
    store.save_integrity_run(
        run_id="integrity:test",
        session_id=session,
        status="collecting",
        row_count=1,
        manifest_hash="e" * 64,
        report={"passed": False},
        created_at=at,
    )
    store.save_factor_run(
        run_id="factor:test",
        session_id=session,
        experiment_id="exp:test",
        status="preregistered",
        spec_hash="f" * 64,
        report={"promotion_allowed": False},
        created_at=at,
    )
    summary = store.summary()
    assert summary["events"] == 3
    assert summary["snapshots"] == 1
    assert summary["trades"] == 1
    assert summary["marks"] == 1
    assert summary["feature_samples"] == 1
    assert summary["integrity_runs"] == 1
    assert summary["factor_runs"] == 1

    read_only = MicrostructureStore(path, read_only=True)
    assert read_only.summary()["events"] == 3
    with pytest.raises(RuntimeError, match="read-only"):
        read_only.initialize()
