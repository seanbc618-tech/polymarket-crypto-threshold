"""Convert persisted Binance public events into continuity-checked L2 tapes."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from sqlite3 import Row
from typing import Any

from crypto_threshold.domain.microstructure import (
    BookSide,
    L2Event,
    L2EventKind,
    L2Level,
    TradeAggressor,
)


class BinanceTapeError(ValueError):
    """A snapshot/delta chain cannot support honest replay."""


@dataclass(frozen=True)
class BuiltBinanceTape:
    symbol: str
    events: tuple[L2Event, ...]
    raw_event_ids: tuple[int, ...]
    payload_hashes: tuple[str, ...]
    snapshot_update_id: int
    final_update_id: int


class BinanceTapeService:
    """Apply Binance's snapshot plus buffered-diff continuity procedure."""

    def build(self, rows: tuple[Row, ...]) -> BuiltBinanceTape:
        if not rows or str(rows[0]["kind"]) != "snapshot":
            raise BinanceTapeError("tape_requires_leading_snapshot")
        snapshot_row = rows[0]
        symbol = str(snapshot_row["symbol"])
        snapshot = _normalized(snapshot_row)
        snapshot_update_id = _required_sequence(
            snapshot_row["venue_sequence_end"],
            "snapshot_update_id",
        )
        bids = _levels(snapshot.get("bids"), allow_zero=False)
        asks = _levels(snapshot.get("asks"), allow_zero=False)
        if not bids or not asks:
            raise BinanceTapeError("snapshot_is_empty")

        depth_rows: list[Row] = []
        trade_rows: list[Row] = []
        previous_final = snapshot_update_id
        bridged = False
        for row in rows[1:]:
            if str(row["symbol"]) != symbol:
                raise BinanceTapeError("mixed_symbol_rows")
            kind = str(row["kind"])
            if kind == "trade":
                trade_rows.append(row)
                continue
            if kind != "depth":
                continue
            first = _required_sequence(row["venue_sequence_start"], "depth_first")
            final = _required_sequence(row["venue_sequence_end"], "depth_final")
            if final <= snapshot_update_id:
                continue
            expected = previous_final + 1
            if not bridged:
                expected = snapshot_update_id + 1
                if not first <= expected <= final:
                    raise BinanceTapeError(
                        f"snapshot_depth_bridge_gap:{first}:{expected}:{final}"
                    )
                bridged = True
            elif first != expected:
                raise BinanceTapeError(
                    f"depth_sequence_gap:{first}:{expected}:{final}"
                )
            depth_rows.append(row)
            previous_final = final
        if not bridged:
            raise BinanceTapeError("no_depth_event_bridges_snapshot")

        normalized_events: list[tuple[datetime, int, int, L2Event]] = []
        raw_ids: set[int] = {int(snapshot_row["event_id"])}
        hashes: set[str] = {str(snapshot_row["payload_hash"])}
        local_sequence = 1
        first_exchange_at = min(
            _time(row["exchange_at"]) for row in (*depth_rows, *trade_rows)
        )
        snapshot_exchange_at = min(
            _time(snapshot_row["received_at"]),
            first_exchange_at - timedelta(microseconds=1),
        )
        snapshot_event = L2Event(
            event_id=f"raw:{int(snapshot_row['event_id'])}:snapshot",
            instrument_id=symbol,
            sequence=local_sequence,
            kind=L2EventKind.SNAPSHOT,
            exchange_at=snapshot_exchange_at,
            received_at=_time(snapshot_row["received_at"]),
            source=str(snapshot_row["source"]),
            source_version=str(snapshot_row["source_version"]),
            payload_hash=str(snapshot_row["payload_hash"]),
            bids=bids,
            asks=asks,
        )
        local_sequence += 1

        pending: list[tuple[datetime, int, int, dict[str, Any], Row]] = []
        for row in depth_rows:
            pending.append(
                (
                    _time(row["exchange_at"]),
                    1,
                    int(row["event_id"]),
                    _normalized(row),
                    row,
                )
            )
        for row in trade_rows:
            pending.append(
                (
                    _time(row["exchange_at"]),
                    0,
                    int(row["event_id"]),
                    _normalized(row),
                    row,
                )
            )
        pending.sort(key=lambda item: (item[0], item[1], item[2]))

        for exchange_at, _, raw_id, normalized, row in pending:
            raw_ids.add(raw_id)
            hashes.add(str(row["payload_hash"]))
            received_at = _time(row["received_at"])
            if received_at < exchange_at:
                raise BinanceTapeError(f"negative_feed_latency:{raw_id}")
            if str(row["kind"]) == "trade":
                price = _decimal(normalized.get("price"), "trade_price")
                quantity = _decimal(normalized.get("quantity"), "trade_quantity")
                aggressor = TradeAggressor(str(normalized.get("aggressor")))
                event = L2Event(
                    event_id=f"raw:{raw_id}:trade",
                    instrument_id=symbol,
                    sequence=local_sequence,
                    kind=L2EventKind.TRADE,
                    exchange_at=exchange_at,
                    received_at=received_at,
                    source=str(row["source"]),
                    source_version=str(row["source_version"]),
                    payload_hash=str(row["payload_hash"]),
                    price=price,
                    quantity=quantity,
                    aggressor=aggressor,
                )
                normalized_events.append((exchange_at, 0, raw_id, event))
                local_sequence += 1
                continue
            for side, key in ((BookSide.BID, "bids"), (BookSide.ASK, "asks")):
                updates = sorted(
                    _levels(normalized.get(key), allow_zero=True),
                    key=lambda level: level.price,
                    reverse=side is BookSide.BID,
                )
                for index, level in enumerate(updates):
                    event = L2Event(
                        event_id=f"raw:{raw_id}:{side.value}:{index}",
                        instrument_id=symbol,
                        sequence=local_sequence,
                        kind=L2EventKind.DEPTH,
                        exchange_at=exchange_at,
                        received_at=received_at,
                        source=str(row["source"]),
                        source_version=str(row["source_version"]),
                        payload_hash=str(row["payload_hash"]),
                        side=side,
                        price=level.price,
                        quantity=level.quantity,
                    )
                    normalized_events.append((exchange_at, 1, raw_id, event))
                    local_sequence += 1

        ordered_events = [snapshot_event]
        ordered_events.extend(item[3] for item in normalized_events)
        resequenced = tuple(
            replace(event, sequence=index)
            for index, event in enumerate(ordered_events, start=1)
        )
        return BuiltBinanceTape(
            symbol=symbol,
            events=resequenced,
            raw_event_ids=tuple(sorted(raw_ids)),
            payload_hashes=tuple(sorted(hashes)),
            snapshot_update_id=snapshot_update_id,
            final_update_id=previous_final,
        )


def _normalized(row: Row) -> dict[str, Any]:
    value = json.loads(str(row["normalized_json"]))
    if not isinstance(value, dict):
        raise BinanceTapeError("normalized_event_must_be_an_object")
    return value


def _levels(value: object, *, allow_zero: bool) -> tuple[L2Level, ...]:
    if not isinstance(value, list):
        raise BinanceTapeError("normalized_levels_must_be_a_list")
    result: list[L2Level] = []
    for item in value:
        if not isinstance(item, dict):
            raise BinanceTapeError("normalized_level_must_be_an_object")
        price = _decimal(item.get("price"), "level_price")
        quantity = _decimal(item.get("quantity"), "level_quantity", allow_zero=True)
        if quantity == 0 and not allow_zero:
            raise BinanceTapeError("snapshot_level_quantity_must_be_positive")
        result.append(L2Level(price=price, quantity=quantity))
    return tuple(result)


def _decimal(
    value: object,
    field: str,
    *,
    allow_zero: bool = False,
) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise BinanceTapeError(f"{field}_is_invalid") from exc
    if result < 0 or (result == 0 and not allow_zero):
        raise BinanceTapeError(f"{field}_must_be_positive")
    return result


def _required_sequence(value: object, field: str) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise BinanceTapeError(f"{field}_is_invalid") from exc
    if result < 0:
        raise BinanceTapeError(f"{field}_must_be_non_negative")
    return result


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BinanceTapeError("stored_timestamp_must_be_timezone_aware")
    return parsed.astimezone(UTC)
