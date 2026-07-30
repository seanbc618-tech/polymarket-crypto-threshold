"""Small HFTBacktest-inspired Level-2 replay for offline research only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from crypto_threshold.domain.microstructure import (
    BookSide,
    FillModel,
    L2Event,
    L2EventKind,
    L2MicrostructureFeatures,
    LatencyProfile,
    LiquidityRole,
    MicrostructureReplayResult,
    MicrostructureSensitivityReport,
    OrderSide,
    QueueModel,
    ReplayAttribution,
    ReplayFill,
    ReplayOrder,
    ReplayOrderType,
    TradeAggressor,
)

HFT_REPLAY_SOURCE_VERSION = "hft-inspired-l2-replay-r1-v1"
_ZERO = Decimal("0")
_ONE = Decimal("1")


class MicrostructureReplayError(ValueError):
    """The event tape or replay intent violates a fail-closed invariant."""


class _Book:
    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    def snapshot(self, event: L2Event) -> None:
        self.bids = {level.price: level.quantity for level in event.bids}
        self.asks = {level.price: level.quantity for level in event.asks}

    def depth(self, event: L2Event) -> tuple[Decimal, Decimal]:
        if event.side is None or event.price is None or event.quantity is None:
            raise MicrostructureReplayError("depth_event_missing_fields")
        levels = self.bids if event.side is BookSide.BID else self.asks
        previous = levels.get(event.price, _ZERO)
        if event.quantity == 0:
            levels.pop(event.price, None)
        else:
            levels[event.price] = event.quantity
        return previous, event.quantity

    def trade(self, event: L2Event) -> None:
        if (
            event.aggressor is None
            or event.price is None
            or event.quantity is None
        ):
            raise MicrostructureReplayError("trade_event_missing_fields")
        levels = self.asks if event.aggressor is TradeAggressor.BUY else self.bids
        previous = levels.get(event.price)
        if previous is None:
            return
        remaining = max(previous - event.quantity, _ZERO)
        if remaining == 0:
            levels.pop(event.price, None)
        else:
            levels[event.price] = remaining

    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def midpoint(self) -> Decimal | None:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / Decimal("2")


class HftReplayService:
    """Reconstruct one L2 tape and replay unsigned, offline order intents."""

    def replay(
        self,
        order: ReplayOrder,
        events: tuple[L2Event, ...],
        *,
        latency: LatencyProfile,
        queue_model: QueueModel = QueueModel.RISK_AVERSE,
        fill_model: FillModel = FillModel.ALL_OR_NOTHING,
    ) -> MicrostructureReplayResult:
        tape = self._validated_tape(order, events)
        self._validate_latency(latency)
        activation_at = _utc(order.submitted_at) + timedelta(
            milliseconds=latency.entry_ms
        )
        acknowledgement_at = activation_at + timedelta(
            milliseconds=latency.response_ms
        )
        book = _Book()
        cursor = 0
        while cursor < len(tape) and _utc(tape[cursor].exchange_at) <= activation_at:
            self._apply_book_event(book, tape[cursor])
            cursor += 1
        if not book.bids or not book.asks:
            raise MicrostructureReplayError("activation_requires_complete_book")
        if (book.best_bid() or _ZERO) >= (book.best_ask() or _ZERO):
            raise MicrostructureReplayError("activation_book_is_crossed")

        arrival_midpoint = book.midpoint()
        remaining = order.quantity
        fills: list[ReplayFill] = []
        resting = False
        queue_ahead: Decimal | None = None
        queue_ahead_at_entry: Decimal | None = None

        if self._is_marketable(order, book):
            taker_fills, remaining = self._walk_taker_depth(
                order,
                book,
                remaining=remaining,
                fill_model=fill_model,
                activation_at=activation_at,
                response_ms=latency.response_ms,
            )
            fills.extend(taker_fills)

        if order.order_type is ReplayOrderType.LIMIT and remaining > 0:
            if order.limit_price is None:
                raise MicrostructureReplayError("limit_order_requires_price")
            own_levels = book.bids if order.side is OrderSide.BUY else book.asks
            queue_ahead = own_levels.get(order.limit_price, _ZERO)
            queue_ahead_at_entry = queue_ahead
            resting = True

        for event in tape[cursor:]:
            if remaining <= 0 or not resting:
                break
            if event.kind is L2EventKind.DEPTH:
                previous, current = book.depth(event)
                if self._is_own_level(order, event):
                    queue_ahead = self._advance_for_depth(
                        queue_ahead or _ZERO,
                        previous=previous,
                        current=current,
                        queue_model=queue_model,
                    )
                continue
            if event.kind is L2EventKind.SNAPSHOT:
                previous = self._own_level_quantity(book, order)
                book.snapshot(event)
                current = self._own_level_quantity(book, order)
                queue_ahead = self._advance_for_depth(
                    queue_ahead or _ZERO,
                    previous=previous,
                    current=current,
                    queue_model=queue_model,
                )
                continue

            available = self._maker_trade_available(
                order,
                event,
                queue_ahead=queue_ahead or _ZERO,
            )
            book.trade(event)
            if available is None:
                continue
            queue_ahead, executable = available
            if executable <= 0:
                continue
            if fill_model is FillModel.ALL_OR_NOTHING and executable < remaining:
                continue
            fill_quantity = min(executable, remaining)
            fill_price = order.limit_price
            if fill_price is None:
                raise MicrostructureReplayError("resting_fill_requires_limit_price")
            fills.append(
                ReplayFill(
                    event_id=event.event_id,
                    exchange_at=_utc(event.exchange_at),
                    received_at=_utc(event.exchange_at)
                    + timedelta(milliseconds=latency.response_ms),
                    price=fill_price,
                    quantity=fill_quantity,
                    liquidity=LiquidityRole.MAKER,
                )
            )
            remaining -= fill_quantity

        return self._result(
            order,
            latency=latency,
            queue_model=queue_model,
            fill_model=fill_model,
            activation_at=activation_at,
            acknowledgement_at=acknowledgement_at,
            arrival_midpoint=arrival_midpoint,
            queue_ahead_at_entry=queue_ahead_at_entry,
            queue_ahead_at_end=queue_ahead,
            fills=tuple(fills),
            remaining=remaining,
        )

    def sensitivity(
        self,
        order: ReplayOrder,
        events: tuple[L2Event, ...],
        *,
        latencies: tuple[LatencyProfile, ...],
        queue_models: tuple[QueueModel, ...] = (
            QueueModel.RISK_AVERSE,
            QueueModel.IDENTITY_PROBABILITY,
            QueueModel.SQUARE_PROBABILITY,
        ),
        fill_models: tuple[FillModel, ...] = (
            FillModel.ALL_OR_NOTHING,
            FillModel.PARTIAL,
        ),
    ) -> MicrostructureSensitivityReport:
        if len(latencies) < 2:
            raise MicrostructureReplayError(
                "sensitivity_requires_multiple_latency_profiles"
            )
        if len(set(latencies)) != len(latencies):
            raise MicrostructureReplayError("duplicate_latency_profile")
        if len({latency.name for latency in latencies}) != len(latencies):
            raise MicrostructureReplayError("duplicate_latency_profile_name")
        if len(set(queue_models)) != len(queue_models):
            raise MicrostructureReplayError("duplicate_queue_model")
        if len(queue_models) < 2 or QueueModel.RISK_AVERSE not in queue_models:
            raise MicrostructureReplayError(
                "sensitivity_requires_risk_averse_and_alternative_queue_models"
            )
        if len(fill_models) < 2 or FillModel.ALL_OR_NOTHING not in fill_models:
            raise MicrostructureReplayError(
                "sensitivity_requires_all_or_nothing_and_partial_fill_models"
            )
        if len(set(fill_models)) != len(fill_models):
            raise MicrostructureReplayError("duplicate_fill_model")

        results = tuple(
            self.replay(
                order,
                events,
                latency=latency,
                queue_model=queue_model,
                fill_model=fill_model,
            )
            for latency in latencies
            for queue_model in queue_models
            for fill_model in fill_models
        )
        marked = [
            result.attribution.net_mark_pnl
            for result in results
            if result.attribution.net_mark_pnl is not None
        ]
        conservative = [
            result
            for result in results
            if result.queue_model is QueueModel.RISK_AVERSE
            and result.fill_model is FillModel.ALL_OR_NOTHING
        ]
        if marked:
            any_positive = any(value > 0 for value in marked)
            conservative_positive = any(
                result.attribution.net_mark_pnl is not None
                and result.attribution.net_mark_pnl > 0
                for result in conservative
            )
            conservative_grid_positive = bool(conservative) and all(
                result.attribution.net_mark_pnl is not None
                and result.attribution.net_mark_pnl > 0
                and result.fill_ratio > 0
                for result in conservative
            )
        else:
            any_positive = any(result.fill_ratio > 0 for result in results)
            conservative_positive = any(
                result.fill_ratio > 0 for result in conservative
            )
            conservative_grid_positive = False
        manifest_hash = _hash(
            {
                "source_version": HFT_REPLAY_SOURCE_VERSION,
                "order": order,
                "events": events,
                "latencies": latencies,
                "queue_models": queue_models,
                "fill_models": fill_models,
                "results": results,
            }
        )
        return MicrostructureSensitivityReport(
            order_id=order.order_id,
            results=results,
            worst_case_net_mark_pnl=min(marked) if marked else None,
            best_case_net_mark_pnl=max(marked) if marked else None,
            minimum_fill_ratio=min(result.fill_ratio for result in results),
            maximum_fill_ratio=max(result.fill_ratio for result in results),
            conservative_grid_positive=conservative_grid_positive,
            optimistic_model_dependency=any_positive and not conservative_positive,
            manifest_hash=manifest_hash,
        )

    def extract_features(
        self,
        events: tuple[L2Event, ...],
        *,
        as_of_event_id: str,
        depth_levels: int = 5,
        trade_lookback: timedelta = timedelta(seconds=5),
    ) -> L2MicrostructureFeatures:
        if depth_levels < 1:
            raise MicrostructureReplayError("depth_levels_must_be_positive")
        if trade_lookback <= timedelta(0):
            raise MicrostructureReplayError("trade_lookback_must_be_positive")
        tape = self._validate_event_tape(events)
        event_index = next(
            (index for index, event in enumerate(tape) if event.event_id == as_of_event_id),
            None,
        )
        if event_index is None:
            raise MicrostructureReplayError("as_of_event_not_found")
        selected = tape[: event_index + 1]
        book = _Book()
        for event in selected:
            self._apply_book_event(book, event)
        bid = book.best_bid()
        ask = book.best_ask()
        if bid is None or ask is None or bid >= ask:
            raise MicrostructureReplayError("feature_extraction_requires_uncrossed_book")

        bid_levels = sorted(book.bids.items(), reverse=True)[:depth_levels]
        ask_levels = sorted(book.asks.items())[:depth_levels]
        bid_depth = sum((quantity for _, quantity in bid_levels), _ZERO)
        ask_depth = sum((quantity for _, quantity in ask_levels), _ZERO)
        total_depth = bid_depth + ask_depth
        if total_depth <= 0:
            raise MicrostructureReplayError("feature_extraction_requires_depth")
        midpoint = (bid + ask) / Decimal("2")
        imbalance = (bid_depth - ask_depth) / total_depth
        microprice = (ask * bid_depth + bid * ask_depth) / total_depth
        paired_levels = tuple(zip(bid_levels, ask_levels, strict=False))
        vamp_denominator = sum(
            (
                bid_quantity + ask_quantity
                for (_, bid_quantity), (_, ask_quantity) in paired_levels
            ),
            _ZERO,
        )
        if vamp_denominator <= 0:
            raise MicrostructureReplayError("feature_extraction_requires_vamp_depth")
        vamp = (
            sum(
                (
                    ask_price * bid_quantity + bid_price * ask_quantity
                    for (bid_price, bid_quantity), (ask_price, ask_quantity) in paired_levels
                ),
                _ZERO,
            )
            / vamp_denominator
        )

        as_of = selected[-1]
        decision_received_at = max(
            _utc(event.received_at) for event in selected
        )
        trade_start = _utc(as_of.exchange_at) - trade_lookback
        buy_volume = _ZERO
        sell_volume = _ZERO
        for event in selected:
            if (
                event.kind is not L2EventKind.TRADE
                or _utc(event.exchange_at) < trade_start
                or event.quantity is None
                or event.aggressor is None
            ):
                continue
            if event.aggressor is TradeAggressor.BUY:
                buy_volume += event.quantity
            else:
                sell_volume += event.quantity
        trade_total = buy_volume + sell_volume
        trade_imbalance = (
            (buy_volume - sell_volume) / trade_total
            if trade_total > 0
            else _ZERO
        )
        feed_latency_ms = Decimal(
            str(
                (
                    _utc(as_of.received_at) - _utc(as_of.exchange_at)
                ).total_seconds()
                * 1000
            )
        )
        return L2MicrostructureFeatures(
            instrument_id=as_of.instrument_id,
            as_of_exchange_at=_utc(as_of.exchange_at),
            as_of_received_at=decision_received_at,
            best_bid=bid,
            best_ask=ask,
            midpoint=midpoint,
            spread=ask - bid,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            book_imbalance=imbalance,
            microprice=microprice,
            vamp=vamp,
            aggressive_trade_imbalance=trade_imbalance,
            feed_latency_ms=feed_latency_ms,
            source_event_ids=tuple(event.event_id for event in selected),
        )

    def _validated_tape(
        self,
        order: ReplayOrder,
        events: tuple[L2Event, ...],
    ) -> tuple[L2Event, ...]:
        tape = self._validate_event_tape(events)
        _require_aware(order.submitted_at, field="order.submitted_at")
        if not order.order_id.strip():
            raise MicrostructureReplayError("order_id_required")
        if not order.strategy_version.strip():
            raise MicrostructureReplayError("strategy_version_required")
        if order.quantity <= 0:
            raise MicrostructureReplayError("order_quantity_must_be_positive")
        if order.order_type is ReplayOrderType.LIMIT:
            if order.limit_price is None or order.limit_price <= 0:
                raise MicrostructureReplayError("limit_order_requires_positive_price")
        elif order.limit_price is not None:
            raise MicrostructureReplayError("market_order_must_not_have_limit_price")
        if order.terminal_mark_price is not None and order.terminal_mark_price <= 0:
            raise MicrostructureReplayError("terminal_mark_price_must_be_positive")
        if not Decimal("-1000") <= order.maker_fee_bps <= Decimal("10000"):
            raise MicrostructureReplayError("maker_fee_bps_out_of_range")
        if not Decimal("-1000") <= order.taker_fee_bps <= Decimal("10000"):
            raise MicrostructureReplayError("taker_fee_bps_out_of_range")
        if any(event.instrument_id != order.instrument_id for event in tape):
            raise MicrostructureReplayError("order_instrument_mismatch")
        decision = next(
            (
                event
                for event in tape
                if event.event_id == order.decision_event_id
            ),
            None,
        )
        if decision is None:
            raise MicrostructureReplayError("decision_event_not_found")
        if _utc(order.submitted_at) < _utc(decision.received_at):
            raise MicrostructureReplayError("order_uses_unreceived_decision_event")
        return tape

    def _validate_event_tape(
        self,
        events: tuple[L2Event, ...],
    ) -> tuple[L2Event, ...]:
        if not events:
            raise MicrostructureReplayError("event_tape_is_empty")
        if events[0].kind is not L2EventKind.SNAPSHOT:
            raise MicrostructureReplayError("event_tape_must_begin_with_snapshot")
        instrument = events[0].instrument_id
        seen_ids: set[str] = set()
        prior_sequence: int | None = None
        prior_exchange_at: datetime | None = None
        for event in events:
            if not event.event_id.strip() or event.event_id in seen_ids:
                raise MicrostructureReplayError("duplicate_or_empty_event_id")
            seen_ids.add(event.event_id)
            if event.instrument_id != instrument:
                raise MicrostructureReplayError("mixed_instrument_event_tape")
            _require_aware(event.exchange_at, field="event.exchange_at")
            _require_aware(event.received_at, field="event.received_at")
            exchange_at = _utc(event.exchange_at)
            received_at = _utc(event.received_at)
            if received_at < exchange_at:
                raise MicrostructureReplayError("negative_feed_latency")
            if not event.source.strip() or not event.source_version.strip():
                raise MicrostructureReplayError("event_source_manifest_missing")
            if not _is_sha256(event.payload_hash):
                raise MicrostructureReplayError("event_payload_hash_is_invalid")
            if prior_sequence is not None and event.sequence <= prior_sequence:
                raise MicrostructureReplayError("non_increasing_event_sequence")
            if prior_exchange_at is not None and exchange_at < prior_exchange_at:
                raise MicrostructureReplayError("non_monotonic_exchange_time")
            prior_sequence = event.sequence
            prior_exchange_at = exchange_at
            self._validate_event_fields(event)
        return events

    @staticmethod
    def _validate_event_fields(event: L2Event) -> None:
        if event.kind is L2EventKind.SNAPSHOT:
            if (
                event.side is not None
                or event.price is not None
                or event.quantity is not None
                or event.aggressor is not None
                or not event.bids
                or not event.asks
            ):
                raise MicrostructureReplayError("malformed_snapshot_event")
            for level in (*event.bids, *event.asks):
                if level.price <= 0 or level.quantity <= 0:
                    raise MicrostructureReplayError("invalid_snapshot_level")
            if len({level.price for level in event.bids}) != len(event.bids):
                raise MicrostructureReplayError("duplicate_snapshot_bid_price")
            if len({level.price for level in event.asks}) != len(event.asks):
                raise MicrostructureReplayError("duplicate_snapshot_ask_price")
            if max(level.price for level in event.bids) >= min(
                level.price for level in event.asks
            ):
                raise MicrostructureReplayError("crossed_snapshot_book")
            return
        if event.bids or event.asks:
            raise MicrostructureReplayError("non_snapshot_event_has_snapshot_levels")
        if event.kind is L2EventKind.DEPTH:
            if (
                event.side is None
                or event.price is None
                or event.quantity is None
                or event.aggressor is not None
                or event.price <= 0
                or event.quantity < 0
            ):
                raise MicrostructureReplayError("malformed_depth_event")
            return
        if (
            event.side is not None
            or event.price is None
            or event.quantity is None
            or event.aggressor is None
            or event.price <= 0
            or event.quantity <= 0
        ):
            raise MicrostructureReplayError("malformed_trade_event")

    @staticmethod
    def _validate_latency(latency: LatencyProfile) -> None:
        if not latency.name.strip():
            raise MicrostructureReplayError("latency_profile_name_required")
        if latency.entry_ms < 0 or latency.response_ms < 0:
            raise MicrostructureReplayError("latency_must_be_non_negative")

    @staticmethod
    def _apply_book_event(book: _Book, event: L2Event) -> None:
        if event.kind is L2EventKind.SNAPSHOT:
            book.snapshot(event)
        elif event.kind is L2EventKind.DEPTH:
            book.depth(event)
        else:
            book.trade(event)

    @staticmethod
    def _is_marketable(order: ReplayOrder, book: _Book) -> bool:
        if order.order_type is ReplayOrderType.MARKET:
            return True
        if order.limit_price is None:
            return False
        if order.side is OrderSide.BUY:
            ask = book.best_ask()
            return ask is not None and ask <= order.limit_price
        bid = book.best_bid()
        return bid is not None and bid >= order.limit_price

    def _walk_taker_depth(
        self,
        order: ReplayOrder,
        book: _Book,
        *,
        remaining: Decimal,
        fill_model: FillModel,
        activation_at: datetime,
        response_ms: int,
    ) -> tuple[list[ReplayFill], Decimal]:
        levels = book.asks if order.side is OrderSide.BUY else book.bids
        prices = sorted(levels, reverse=order.side is OrderSide.SELL)
        eligible = [
            price
            for price in prices
            if self._price_is_executable(order, price)
        ]
        available = sum((levels[price] for price in eligible), _ZERO)
        if fill_model is FillModel.ALL_OR_NOTHING and available < remaining:
            return [], remaining
        fills: list[ReplayFill] = []
        for index, price in enumerate(eligible):
            if remaining <= 0:
                break
            quantity = min(levels[price], remaining)
            if quantity <= 0:
                continue
            fills.append(
                ReplayFill(
                    event_id=f"activation:{order.order_id}:{index}",
                    exchange_at=activation_at,
                    received_at=activation_at
                    + timedelta(milliseconds=response_ms),
                    price=price,
                    quantity=quantity,
                    liquidity=LiquidityRole.TAKER,
                )
            )
            remaining -= quantity
            level_remaining = levels[price] - quantity
            if level_remaining <= 0:
                levels.pop(price, None)
            else:
                levels[price] = level_remaining
        return fills, remaining

    @staticmethod
    def _price_is_executable(order: ReplayOrder, price: Decimal) -> bool:
        if order.order_type is ReplayOrderType.MARKET:
            return True
        if order.limit_price is None:
            return False
        if order.side is OrderSide.BUY:
            return price <= order.limit_price
        return price >= order.limit_price

    @staticmethod
    def _is_own_level(order: ReplayOrder, event: L2Event) -> bool:
        if event.price != order.limit_price:
            return False
        expected = BookSide.BID if order.side is OrderSide.BUY else BookSide.ASK
        return event.side is expected

    @staticmethod
    def _own_level_quantity(book: _Book, order: ReplayOrder) -> Decimal:
        if order.limit_price is None:
            return _ZERO
        levels = book.bids if order.side is OrderSide.BUY else book.asks
        return levels.get(order.limit_price, _ZERO)

    @staticmethod
    def _advance_for_depth(
        queue_ahead: Decimal,
        *,
        previous: Decimal,
        current: Decimal,
        queue_model: QueueModel,
    ) -> Decimal:
        queue_ahead = min(max(queue_ahead, _ZERO), max(previous, _ZERO))
        if current >= previous:
            return min(queue_ahead, current)
        decrease = previous - current
        if queue_model is QueueModel.RISK_AVERSE:
            return min(queue_ahead, current)
        front = queue_ahead
        back = max(previous - front, _ZERO)
        if front + back <= 0:
            return _ZERO
        if queue_model is QueueModel.IDENTITY_PROBABILITY:
            front_weight = front
            back_weight = back
        else:
            front_weight = front * front
            back_weight = back * back
        denominator = front_weight + back_weight
        probability_after = (
            back_weight / denominator if denominator > 0 else _ZERO
        )
        estimated_front = (
            front
            - (_ONE - probability_after) * decrease
            + min(back - probability_after * decrease, _ZERO)
        )
        return min(max(estimated_front, _ZERO), current)

    @staticmethod
    def _maker_trade_available(
        order: ReplayOrder,
        event: L2Event,
        *,
        queue_ahead: Decimal,
    ) -> tuple[Decimal, Decimal] | None:
        if (
            event.kind is not L2EventKind.TRADE
            or event.price is None
            or event.quantity is None
            or order.limit_price is None
        ):
            return None
        if order.side is OrderSide.BUY:
            if event.aggressor is not TradeAggressor.SELL:
                return None
            if event.price > order.limit_price:
                return None
            exact_level = event.price == order.limit_price
        else:
            if event.aggressor is not TradeAggressor.BUY:
                return None
            if event.price < order.limit_price:
                return None
            exact_level = event.price == order.limit_price
        if not exact_level:
            return _ZERO, event.quantity
        consumed_ahead = min(queue_ahead, event.quantity)
        new_queue = queue_ahead - consumed_ahead
        return new_queue, event.quantity - consumed_ahead

    @staticmethod
    def _result(
        order: ReplayOrder,
        *,
        latency: LatencyProfile,
        queue_model: QueueModel,
        fill_model: FillModel,
        activation_at: datetime,
        acknowledgement_at: datetime,
        arrival_midpoint: Decimal | None,
        queue_ahead_at_entry: Decimal | None,
        queue_ahead_at_end: Decimal | None,
        fills: tuple[ReplayFill, ...],
        remaining: Decimal,
    ) -> MicrostructureReplayResult:
        filled = order.quantity - remaining
        notional = sum((fill.price * fill.quantity for fill in fills), _ZERO)
        average = notional / filled if filled > 0 else None
        fee = sum(
            (
                fill.price
                * fill.quantity
                * (
                    order.maker_fee_bps
                    if fill.liquidity is LiquidityRole.MAKER
                    else order.taker_fee_bps
                )
                / Decimal("10000")
                for fill in fills
            ),
            _ZERO,
        )
        sign = _ONE if order.side is OrderSide.BUY else -_ONE
        if (
            average is not None
            and arrival_midpoint is not None
            and order.terminal_mark_price is not None
        ):
            spread_capture = sign * (arrival_midpoint - average) * filled
            directional = (
                sign
                * (order.terminal_mark_price - arrival_midpoint)
                * filled
            )
            gross = spread_capture + directional
            net = gross - fee
        else:
            spread_capture = None
            directional = None
            gross = None
            net = None
        if remaining == 0:
            status = "filled"
            reasons: tuple[str, ...] = ()
        elif filled > 0:
            status = "partially_filled"
            reasons = ("residual_quantity_unfilled",)
        else:
            status = "unfilled"
            reasons = (
                "insufficient_executable_depth"
                if order.order_type is ReplayOrderType.MARKET
                else "resting_order_not_filled",
            )
        return MicrostructureReplayResult(
            order_id=order.order_id,
            latency_profile=latency.name,
            queue_model=queue_model,
            fill_model=fill_model,
            activation_at=activation_at,
            acknowledgement_at=acknowledgement_at,
            completed_at=max((fill.received_at for fill in fills), default=None),
            filled_quantity=filled,
            remaining_quantity=remaining,
            fill_ratio=filled / order.quantity,
            average_fill_price=average,
            total_notional=notional,
            queue_ahead_at_entry=queue_ahead_at_entry,
            queue_ahead_at_end=queue_ahead_at_end,
            fills=fills,
            attribution=ReplayAttribution(
                arrival_midpoint=arrival_midpoint,
                terminal_mark_price=order.terminal_mark_price,
                spread_capture=spread_capture,
                directional_component=directional,
                gross_mark_pnl=gross,
                fee_cost=fee,
                net_mark_pnl=net,
                residual_inventory_quantity=filled,
            ),
            status=status,
            reasons=reasons,
        )


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MicrostructureReplayError(f"{field}_must_be_timezone_aware")


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _hash(value: object) -> str:
    encoded = json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _canonical(value: object) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _canonical(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value
