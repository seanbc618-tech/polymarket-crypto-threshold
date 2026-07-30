"""Deterministic tests for the HFTBacktest-inspired Level-2 replay core."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_threshold.domain.microstructure import (
    BookSide,
    FillModel,
    L2Event,
    L2EventKind,
    L2Level,
    LatencyProfile,
    OrderSide,
    QueueModel,
    ReplayOrder,
    ReplayOrderType,
    TradeAggressor,
)
from crypto_threshold.services.hft_replay_service import (
    HftReplayService,
    MicrostructureReplayError,
)

BASE = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
FAST = LatencyProfile(name="fast", entry_ms=0, response_ms=10)


def _snapshot(
    *,
    bids: tuple[tuple[str, str], ...] = (("99", "10"), ("98", "10")),
    asks: tuple[tuple[str, str], ...] = (("101", "4"), ("102", "10")),
    received_ms: int = 50,
) -> L2Event:
    return L2Event(
        event_id="snapshot",
        instrument_id="BTCUSDT",
        sequence=1,
        kind=L2EventKind.SNAPSHOT,
        exchange_at=BASE,
        received_at=BASE + timedelta(milliseconds=received_ms),
        source="binance_spot",
        source_version="test-l2-v1",
        payload_hash=f"{1:064x}",
        bids=tuple(
            L2Level(price=Decimal(price), quantity=Decimal(quantity))
            for price, quantity in bids
        ),
        asks=tuple(
            L2Level(price=Decimal(price), quantity=Decimal(quantity))
            for price, quantity in asks
        ),
    )


def _depth(
    sequence: int,
    *,
    side: BookSide,
    price: str,
    quantity: str,
    at_ms: int,
    event_id: str | None = None,
) -> L2Event:
    return L2Event(
        event_id=event_id or f"depth-{sequence}",
        instrument_id="BTCUSDT",
        sequence=sequence,
        kind=L2EventKind.DEPTH,
        exchange_at=BASE + timedelta(milliseconds=at_ms),
        received_at=BASE + timedelta(milliseconds=at_ms + 50),
        source="binance_spot",
        source_version="test-l2-v1",
        payload_hash=f"{sequence:064x}",
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
    )


def _trade(
    sequence: int,
    *,
    aggressor: TradeAggressor,
    price: str,
    quantity: str,
    at_ms: int,
    event_id: str | None = None,
) -> L2Event:
    return L2Event(
        event_id=event_id or f"trade-{sequence}",
        instrument_id="BTCUSDT",
        sequence=sequence,
        kind=L2EventKind.TRADE,
        exchange_at=BASE + timedelta(milliseconds=at_ms),
        received_at=BASE + timedelta(milliseconds=at_ms + 50),
        source="binance_spot",
        source_version="test-trade-v1",
        payload_hash=f"{sequence:064x}",
        aggressor=aggressor,
        price=Decimal(price),
        quantity=Decimal(quantity),
    )


def _order(**updates: object) -> ReplayOrder:
    values: dict[str, object] = {
        "order_id": "order-1",
        "strategy_version": "test-strategy-v1",
        "instrument_id": "BTCUSDT",
        "side": OrderSide.BUY,
        "order_type": ReplayOrderType.LIMIT,
        "quantity": Decimal("2"),
        "submitted_at": BASE + timedelta(milliseconds=50),
        "decision_event_id": "snapshot",
        "limit_price": Decimal("99"),
    }
    values.update(updates)
    return ReplayOrder(**values)  # type: ignore[arg-type]


def test_feed_and_order_latency_change_the_exchange_book_seen_at_activation() -> None:
    events = (
        _snapshot(),
        _depth(
            2,
            side=BookSide.ASK,
            price="101",
            quantity="0",
            at_ms=100,
        ),
    )
    order = _order(
        order_type=ReplayOrderType.MARKET,
        quantity=Decimal("5"),
        limit_price=None,
    )
    service = HftReplayService()

    fast = service.replay(
        order,
        events,
        latency=FAST,
        fill_model=FillModel.PARTIAL,
    )
    slow = service.replay(
        order,
        events,
        latency=LatencyProfile(name="slow", entry_ms=200, response_ms=40),
        fill_model=FillModel.PARTIAL,
    )

    assert fast.status == "filled"
    assert fast.average_fill_price == Decimal("101.2")
    assert slow.status == "filled"
    assert slow.average_fill_price == Decimal("102")
    assert slow.acknowledgement_at == BASE + timedelta(milliseconds=290)


def test_risk_averse_queue_does_not_use_probabilistic_cancellation_credit() -> None:
    events = (
        _snapshot(asks=(("101", "10"),)),
        _depth(
            2,
            side=BookSide.BID,
            price="99",
            quantity="30",
            at_ms=100,
        ),
        _depth(
            3,
            side=BookSide.BID,
            price="99",
            quantity="20",
            at_ms=200,
        ),
        _trade(
            4,
            aggressor=TradeAggressor.SELL,
            price="99",
            quantity="7",
            at_ms=300,
        ),
    )
    service = HftReplayService()

    risk = service.replay(
        _order(),
        events,
        latency=FAST,
        queue_model=QueueModel.RISK_AVERSE,
        fill_model=FillModel.PARTIAL,
    )
    identity = service.replay(
        _order(),
        events,
        latency=FAST,
        queue_model=QueueModel.IDENTITY_PROBABILITY,
        fill_model=FillModel.PARTIAL,
    )
    square = service.replay(
        _order(),
        events,
        latency=FAST,
        queue_model=QueueModel.SQUARE_PROBABILITY,
        fill_model=FillModel.PARTIAL,
    )

    assert risk.filled_quantity == 0
    assert risk.queue_ahead_at_entry == Decimal("10")
    assert identity.filled_quantity > 0
    assert square.filled_quantity == 0
    assert identity.queue_ahead_at_end < square.queue_ahead_at_end


def test_same_price_trades_advance_risk_averse_queue_before_fill() -> None:
    events = (
        _snapshot(asks=(("101", "10"),)),
        _trade(
            2,
            aggressor=TradeAggressor.SELL,
            price="99",
            quantity="6",
            at_ms=100,
        ),
        _trade(
            3,
            aggressor=TradeAggressor.SELL,
            price="99",
            quantity="6",
            at_ms=200,
        ),
    )

    result = HftReplayService().replay(
        _order(),
        events,
        latency=FAST,
        queue_model=QueueModel.RISK_AVERSE,
        fill_model=FillModel.PARTIAL,
    )

    assert result.status == "filled"
    assert result.filled_quantity == Decimal("2")
    assert len(result.fills) == 1
    assert result.fills[0].event_id == "trade-3"


def test_all_or_nothing_fails_closed_when_taker_depth_is_insufficient() -> None:
    events = (_snapshot(asks=(("101", "4"),)),)
    order = _order(
        order_type=ReplayOrderType.MARKET,
        quantity=Decimal("5"),
        limit_price=None,
    )
    service = HftReplayService()

    conservative = service.replay(
        order,
        events,
        latency=FAST,
        fill_model=FillModel.ALL_OR_NOTHING,
    )
    partial = service.replay(
        order,
        events,
        latency=FAST,
        fill_model=FillModel.PARTIAL,
    )

    assert conservative.status == "unfilled"
    assert conservative.filled_quantity == 0
    assert partial.status == "partially_filled"
    assert partial.filled_quantity == Decimal("4")


def test_marked_pnl_is_split_into_execution_direction_and_fees() -> None:
    result = HftReplayService().replay(
        _order(
            order_type=ReplayOrderType.MARKET,
            quantity=Decimal("2"),
            limit_price=None,
            taker_fee_bps=Decimal("10"),
            terminal_mark_price=Decimal("103"),
        ),
        (_snapshot(asks=(("101", "4"),)),),
        latency=FAST,
        fill_model=FillModel.PARTIAL,
    )

    assert result.attribution.arrival_midpoint == Decimal("100")
    assert result.attribution.spread_capture == Decimal("-2")
    assert result.attribution.directional_component == Decimal("6")
    assert result.attribution.gross_mark_pnl == Decimal("4")
    assert result.attribution.fee_cost == Decimal("0.202")
    assert result.attribution.net_mark_pnl == Decimal("3.798")


def test_sensitivity_requires_multiple_models_and_reports_optimistic_dependency() -> None:
    events = (
        _snapshot(asks=(("101", "10"),)),
        _depth(
            2,
            side=BookSide.BID,
            price="99",
            quantity="30",
            at_ms=100,
        ),
        _depth(
            3,
            side=BookSide.BID,
            price="99",
            quantity="20",
            at_ms=200,
        ),
        _trade(
            4,
            aggressor=TradeAggressor.SELL,
            price="99",
            quantity="8",
            at_ms=300,
        ),
    )
    order = _order(terminal_mark_price=Decimal("105"))
    service = HftReplayService()
    report = service.sensitivity(
        order,
        events,
        latencies=(
            FAST,
            LatencyProfile(name="slower", entry_ms=20, response_ms=30),
        ),
    )
    repeated = service.sensitivity(
        order,
        events,
        latencies=(
            FAST,
            LatencyProfile(name="slower", entry_ms=20, response_ms=30),
        ),
    )

    assert len(report.results) == 12
    assert report.optimistic_model_dependency is True
    assert report.minimum_fill_ratio == 0
    assert report.maximum_fill_ratio > 0
    assert report.conservative_grid_positive is False
    assert len(report.manifest_hash) == 64
    assert repeated.manifest_hash == report.manifest_hash

    with pytest.raises(
        MicrostructureReplayError,
        match="multiple_latency_profiles",
    ):
        service.sensitivity(order, events, latencies=(FAST,))


def test_feature_extraction_records_l2_and_aggressive_trade_imbalance() -> None:
    events = (
        _snapshot(bids=(("99", "10"),), asks=(("101", "5"),)),
        _trade(
            2,
            aggressor=TradeAggressor.BUY,
            price="101",
            quantity="2",
            at_ms=100,
        ),
        _trade(
            3,
            aggressor=TradeAggressor.SELL,
            price="99",
            quantity="1",
            at_ms=200,
        ),
        _depth(
            4,
            side=BookSide.ASK,
            price="101",
            quantity="4",
            at_ms=300,
            event_id="as-of",
        ),
    )

    features = HftReplayService().extract_features(
        events,
        as_of_event_id="as-of",
        depth_levels=1,
        trade_lookback=timedelta(seconds=1),
    )

    assert features.bid_depth == Decimal("9")
    assert features.ask_depth == Decimal("4")
    assert features.book_imbalance == Decimal("5") / Decimal("13")
    assert features.microprice == (
        Decimal("101") * Decimal("9") + Decimal("99") * Decimal("4")
    ) / Decimal("13")
    assert features.vamp == features.microprice
    assert features.aggressive_trade_imbalance == Decimal("1") / Decimal("3")
    assert features.feed_latency_ms == Decimal("50.0")
    assert features.source_event_ids == ("snapshot", "trade-2", "trade-3", "as-of")


def test_invalid_tapes_and_unreceived_decision_inputs_fail_closed() -> None:
    snapshot = _snapshot()
    with pytest.raises(MicrostructureReplayError, match="negative_feed_latency"):
        HftReplayService().replay(
            _order(),
            (
                replace(
                    snapshot,
                    received_at=BASE - timedelta(milliseconds=1),
                ),
            ),
            latency=FAST,
        )

    with pytest.raises(
        MicrostructureReplayError,
        match="unreceived_decision_event",
    ):
        HftReplayService().replay(
            _order(submitted_at=BASE + timedelta(milliseconds=49)),
            (snapshot,),
            latency=FAST,
        )

    with pytest.raises(MicrostructureReplayError, match="payload_hash"):
        HftReplayService().replay(
            _order(),
            (replace(snapshot, payload_hash="not-a-sha256"),),
            latency=FAST,
        )

    with pytest.raises(MicrostructureReplayError, match="strategy_version"):
        HftReplayService().replay(
            _order(strategy_version=""),
            (snapshot,),
            latency=FAST,
        )
