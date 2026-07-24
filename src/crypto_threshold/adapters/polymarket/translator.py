"""Translate public Gamma and CLOB payloads into domain objects."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from crypto_threshold.domain.markets import (
    CryptoMarket,
    OrderBookLevel,
    OrderBookSnapshot,
)

GAMMA_SOURCE_VERSION = "gamma-markets-v1"
CLOB_BOOK_SOURCE_VERSION = "clob-book-v1"


def translate_market(
    payload: dict[str, Any], *, received_at: datetime | None = None
) -> CryptoMarket:
    received_at = _as_utc(received_at or datetime.now(UTC))
    events = _jsonish_list(payload.get("events"))
    event = events[0] if events and isinstance(events[0], dict) else {}
    event_id = _string(payload.get("eventId") or event.get("id"))
    outcomes = tuple(str(item) for item in _jsonish_list(payload.get("outcomes")))
    token_ids = _jsonish_list(payload.get("clobTokenIds"))
    if not token_ids:
        token_ids = _jsonish_list(payload.get("tokenIds"))
    yes_token, no_token = _yes_no_tokens(outcomes, token_ids, payload.get("tokens"))
    market_id = _string(payload.get("id") or payload.get("conditionId")) or ""
    return CryptoMarket(
        market_id=market_id,
        event_id=event_id,
        condition_id=_string(payload.get("conditionId")),
        question=str(payload.get("question") or payload.get("title") or "").strip(),
        slug=_string(payload.get("slug")),
        description=_string(
            payload.get("description") or payload.get("rules") or payload.get("resolutionSource")
        ),
        active=_optional_boolean(payload.get("active")),
        closed=_optional_boolean(payload.get("closed")),
        accepting_orders=_optional_boolean(payload.get("acceptingOrders")),
        enable_order_book=_optional_boolean(payload.get("enableOrderBook")),
        gamma_end_date=_datetime(
            payload.get("endDate") or payload.get("endDateIso") or payload.get("end_date_iso")
        ),
        outcomes=outcomes,
        yes_token_id=yes_token,
        no_token_id=no_token,
        received_at=received_at,
        raw_payload=payload,
    )


def translate_order_book(
    *,
    market_id: str,
    token_id: str,
    outcome: str,
    payload: dict[str, Any],
    received_at: datetime | None = None,
) -> OrderBookSnapshot:
    received_at = _as_utc(received_at or datetime.now(UTC))
    bids = _levels(payload.get("bids"), reverse=True)
    asks = _levels(payload.get("asks"), reverse=False)
    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    midpoint = (
        (best_bid + best_ask) / Decimal("2")
        if best_bid is not None and best_ask is not None
        else None
    )
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    observed_at, trusted = _book_timestamp(payload.get("timestamp"), received_at)
    return OrderBookSnapshot(
        market_id=market_id,
        token_id=token_id,
        outcome=outcome.upper(),
        bids=bids,
        asks=asks,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        bid_depth=sum((level.price * level.size for level in bids), Decimal("0")),
        ask_depth=sum((level.price * level.size for level in asks), Decimal("0")),
        observed_at=observed_at,
        received_at=received_at,
        timestamp_trusted=trusted,
        source_version=CLOB_BOOK_SOURCE_VERSION,
        raw_payload=payload,
    )


def _yes_no_tokens(
    outcomes: tuple[str, ...], token_ids: list[Any], raw_tokens: Any
) -> tuple[str | None, str | None]:
    normalized = [outcome.strip().lower() for outcome in outcomes]
    if len(token_ids) >= 2 and "yes" in normalized and "no" in normalized:
        return (
            _string(token_ids[normalized.index("yes")]),
            _string(token_ids[normalized.index("no")]),
        )
    tokens = _jsonish_list(raw_tokens)
    mapped: dict[str, str] = {}
    for token in tokens:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome") or "").strip().lower()
        token_id = _string(token.get("token_id") or token.get("tokenId") or token.get("id"))
        if outcome in {"yes", "no"} and token_id:
            mapped[outcome] = token_id
    return mapped.get("yes"), mapped.get("no")


def _levels(raw: Any, *, reverse: bool) -> tuple[OrderBookLevel, ...]:
    levels: list[OrderBookLevel] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if isinstance(item, dict):
            price_raw = item.get("price")
            size_raw = item.get("size") or item.get("quantity")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price_raw, size_raw = item[0], item[1]
        else:
            continue
        try:
            price = Decimal(str(price_raw))
            size = Decimal(str(size_raw))
        except (InvalidOperation, ValueError):
            continue
        if price < 0 or price > 1 or size <= 0:
            continue
        levels.append(OrderBookLevel(price=price, size=size))
    return tuple(sorted(levels, key=lambda level: level.price, reverse=reverse))


def _jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _book_timestamp(value: Any, fallback: datetime) -> tuple[datetime, bool]:
    if value is None or value == "":
        return fallback, False
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=UTC), True
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _as_utc(parsed), True
    except (ValueError, OSError, OverflowError):
        return fallback, False


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _optional_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
