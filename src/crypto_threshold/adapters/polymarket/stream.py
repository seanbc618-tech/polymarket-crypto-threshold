"""Read-only Polymarket stream bridge over the official public SDK API.

Adapted from the validated ``PolymarketStreamBridge`` in polymarket-weather-arb.
The local copy avoids a runtime dependency on the sibling repository and replaces
weather grouping with crypto event/settlement-contract ladders.

The bridge owns one daemon thread and one asyncio loop. It never opens SQLite,
runs strategy code, treats User Channel events as fills, or mutates orders.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

STREAM_CANDIDATE_GROUP_CAP = 4
MARKET_TOKEN_STALE_SECONDS = 45.0
STREAM_REST_VERIFY_SECONDS = 90.0
STREAM_MAX_QUOTE_SLOTS = 512
STREAM_MAX_DIAGNOSTIC_EVENTS = 64
_SHUTDOWN_JOIN_SECONDS = 5.0
_RECONNECT_DELAY_SECONDS = 0.5

StreamStatus = str
CryptoLadderKey = tuple[str, ...]


@dataclass(frozen=True)
class StreamQuote:
    """Normalized token-level top of book; not executable depth."""

    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None
    spread: Decimal | None
    liquidity: Decimal | None
    received_at: float
    source_type: str
    condition_id: str | None = None


@dataclass(frozen=True)
class StreamUserHint:
    """Account activity hint that can only request REST reconciliation."""

    kind: str
    received_at: float
    event_type: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class StreamTickSizeHint:
    token_id: str
    received_at: float
    condition_id: str | None = None


@dataclass(frozen=True)
class StreamResolvedHint:
    condition_id: str
    received_at: float
    token_ids: tuple[str, ...] = ()


NormalizedStreamEvent = StreamQuote | StreamUserHint | StreamTickSizeHint | StreamResolvedHint


@dataclass
class StreamDrainBatch:
    """Coalesced hints for a serial workflow pulse."""

    quotes: dict[str, StreamQuote] = field(default_factory=dict)
    reconcile_due: bool = False
    user_hints: list[StreamUserHint] = field(default_factory=list)
    tick_size: list[StreamTickSizeHint] = field(default_factory=list)
    resolved: list[StreamResolvedHint] = field(default_factory=list)
    dropped: int = 0
    coalesced: int = 0


@dataclass
class StreamHealth:
    status: StreamStatus = "disabled"
    updated_at: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        """Return a recursively scrubbed health snapshot."""
        return {
            "status": self.status,
            "updated_at": self.updated_at,
            "detail": _scrub_secret_keys(self.detail),
        }


@dataclass(frozen=True)
class DesiredSubscription:
    """Derived Market Channel subscription; never persisted as exchange truth."""

    token_ids: tuple[str, ...]
    token_to_market: Mapping[str, str]
    held_tokens: frozenset[str]
    open_order_tokens: frozenset[str]
    active_ladder_tokens: frozenset[str]
    candidate_tokens: frozenset[str]
    selected_ladders: tuple[CryptoLadderKey, ...]

    def __bool__(self) -> bool:
        return bool(self.token_ids)


def _scrub_secret_keys(value: Any) -> Any:
    secret_fragments = ("key", "secret", "credential", "private", "auth", "password")
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_secret_keys(item)
            for key, item in value.items()
            if not any(fragment in str(key).lower() for fragment in secret_fragments)
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_secret_keys(item) for item in value]
    return value


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _mid_spread(
    best_bid: Decimal | None, best_ask: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / Decimal("2"), best_ask - best_bid
    return None, None


def normalize_sdk_events(event: Any) -> list[NormalizedStreamEvent]:
    """Normalize typed official SDK events into token-level hints."""
    topic = getattr(event, "topic", None)
    event_type = getattr(event, "type", None)
    payload = getattr(event, "payload", None)
    if payload is None:
        return []
    received_at = time.monotonic()

    if topic == "market" or event_type in {
        "book",
        "best_bid_ask",
        "price_change",
        "tick_size_change",
        "market_resolved",
    }:
        if event_type == "book":
            token_id = str(getattr(payload, "token_id", "") or "")
            if not token_id:
                return []
            bids = getattr(payload, "bids", ()) or ()
            asks = getattr(payload, "asks", ()) or ()
            bid_levels = [
                (
                    _decimal_or_none(getattr(level, "price", None)),
                    _decimal_or_none(getattr(level, "size", None)),
                )
                for level in bids
            ]
            ask_levels = [
                (
                    _decimal_or_none(getattr(level, "price", None)),
                    _decimal_or_none(getattr(level, "size", None)),
                )
                for level in asks
            ]
            valid_bids = [(price, size) for price, size in bid_levels if price is not None]
            valid_asks = [(price, size) for price, size in ask_levels if price is not None]
            best_bid = max((price for price, _ in valid_bids), default=None)
            best_ask = min((price for price, _ in valid_asks), default=None)
            bid_liquidity = sum((size or Decimal("0") for _, size in valid_bids), Decimal("0"))
            ask_liquidity = sum((size or Decimal("0") for _, size in valid_asks), Decimal("0"))
            midpoint, spread = _mid_spread(best_bid, best_ask)
            return [
                StreamQuote(
                    token_id=token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    midpoint=midpoint,
                    spread=spread,
                    liquidity=(bid_liquidity + ask_liquidity) if bids or asks else None,
                    received_at=received_at,
                    source_type="book",
                    condition_id=str(getattr(payload, "market", "") or "") or None,
                )
            ]
        if event_type == "best_bid_ask":
            token_id = str(getattr(payload, "token_id", "") or "")
            if not token_id:
                return []
            best_bid = _decimal_or_none(getattr(payload, "best_bid", None))
            best_ask = _decimal_or_none(getattr(payload, "best_ask", None))
            supplied_spread = _decimal_or_none(getattr(payload, "spread", None))
            midpoint, spread = _mid_spread(best_bid, best_ask)
            return [
                StreamQuote(
                    token_id=token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    midpoint=midpoint,
                    spread=supplied_spread if supplied_spread is not None else spread,
                    liquidity=None,
                    received_at=received_at,
                    source_type="best_bid_ask",
                    condition_id=str(getattr(payload, "market", "") or "") or None,
                )
            ]
        if event_type == "price_change":
            condition_id = str(getattr(payload, "market", "") or "") or None
            quotes: list[NormalizedStreamEvent] = []
            for change in getattr(payload, "price_changes", ()) or ():
                token_id = str(getattr(change, "token_id", "") or "")
                best_bid = _decimal_or_none(getattr(change, "best_bid", None))
                best_ask = _decimal_or_none(getattr(change, "best_ask", None))
                if not token_id or (best_bid is None and best_ask is None):
                    continue
                midpoint, spread = _mid_spread(best_bid, best_ask)
                quotes.append(
                    StreamQuote(
                        token_id=token_id,
                        best_bid=best_bid,
                        best_ask=best_ask,
                        midpoint=midpoint,
                        spread=spread,
                        liquidity=None,
                        received_at=received_at,
                        source_type="price_change",
                        condition_id=condition_id,
                    )
                )
            return quotes
        if event_type == "tick_size_change":
            token_id = str(getattr(payload, "token_id", "") or "")
            if not token_id:
                return []
            return [
                StreamTickSizeHint(
                    token_id=token_id,
                    received_at=received_at,
                    condition_id=str(getattr(payload, "market", "") or "") or None,
                )
            ]
        if event_type == "market_resolved":
            condition_id = str(
                getattr(payload, "market", None) or getattr(payload, "id", None) or ""
            )
            if not condition_id:
                return []
            raw_tokens = getattr(payload, "token_ids", None) or ()
            return [
                StreamResolvedHint(
                    condition_id=condition_id,
                    received_at=received_at,
                    token_ids=tuple(str(token) for token in raw_tokens if token is not None),
                )
            ]

    if topic == "user" or event_type in {"order", "trade"}:
        if event_type == "order":
            return [
                StreamUserHint(
                    kind="order",
                    received_at=received_at,
                    event_type=str(getattr(payload, "order_event_type", "") or "") or None,
                    status=str(getattr(payload, "status", "") or "") or None,
                )
            ]
        if event_type == "trade":
            return [
                StreamUserHint(
                    kind="trade",
                    received_at=received_at,
                    event_type="trade",
                    status=str(getattr(payload, "status", "") or "") or None,
                )
            ]
    return []


def normalize_sdk_event(event: Any) -> NormalizedStreamEvent | None:
    """Compatibility wrapper returning the first normalized event."""
    items = normalize_sdk_events(event)
    return items[0] if items else None


class _CoalescingQueue:
    """Bounded queue with one pending and one remembered quote per token."""

    def __init__(
        self,
        *,
        max_quote_slots: int = STREAM_MAX_QUOTE_SLOTS,
        max_diagnostic_events: int = STREAM_MAX_DIAGNOSTIC_EVENTS,
    ) -> None:
        self._lock = threading.Lock()
        self._max_quote_slots = max(1, int(max_quote_slots))
        self._max_diagnostic_events = max(1, int(max_diagnostic_events))
        self._quotes: OrderedDict[str, StreamQuote] = OrderedDict()
        self._last_quotes: OrderedDict[str, StreamQuote] = OrderedDict()
        self._reconcile_due = False
        self._user_hints: list[StreamUserHint] = []
        self._tick_size: list[StreamTickSizeHint] = []
        self._resolved: list[StreamResolvedHint] = []
        self.dropped = 0
        self.coalesced = 0
        self.unchanged_quotes = 0
        self.ignored_quotes = 0
        self.unknown_events = 0
        self.market_events = 0
        self.user_events = 0

    def publish(self, item: Any) -> bool:
        with self._lock:
            if item is None:
                self.unknown_events += 1
                return False
            if isinstance(item, StreamQuote):
                self.market_events += 1
                previous = self._last_quotes.get(item.token_id)
                if previous is not None and _same_effective_quote(previous, item):
                    self.unchanged_quotes += 1
                    self._last_quotes.move_to_end(item.token_id)
                    return False
                if (
                    item.token_id not in self._last_quotes
                    and len(self._last_quotes) >= self._max_quote_slots
                ):
                    evicted, _ = self._last_quotes.popitem(last=False)
                    if self._quotes.pop(evicted, None) is not None:
                        self.dropped += 1
                if item.token_id in self._quotes:
                    self.coalesced += 1
                self._last_quotes[item.token_id] = item
                self._last_quotes.move_to_end(item.token_id)
                self._quotes[item.token_id] = item
                self._quotes.move_to_end(item.token_id)
                return True
            if isinstance(item, StreamUserHint):
                self.user_events += 1
                if self._reconcile_due:
                    self.coalesced += 1
                self._reconcile_due = True
                self._append_diagnostic(self._user_hints, item)
                return True
            if isinstance(item, StreamTickSizeHint):
                self.market_events += 1
                self._append_diagnostic(self._tick_size, item)
                return True
            if isinstance(item, StreamResolvedHint):
                self.market_events += 1
                self._reconcile_due = True
                self._append_diagnostic(self._resolved, item)
                return True
            self.unknown_events += 1
            return False

    def _append_diagnostic(self, target: list[Any], item: Any) -> None:
        if len(target) < self._max_diagnostic_events:
            target.append(item)
        else:
            self.dropped += 1

    def ignore_quote(self) -> None:
        with self._lock:
            self.ignored_quotes += 1

    def drain(self) -> StreamDrainBatch:
        with self._lock:
            batch = StreamDrainBatch(
                quotes=dict(self._quotes),
                reconcile_due=self._reconcile_due,
                user_hints=list(self._user_hints),
                tick_size=list(self._tick_size),
                resolved=list(self._resolved),
                dropped=self.dropped,
                coalesced=self.coalesced,
            )
            self._quotes.clear()
            self._reconcile_due = False
            self._user_hints.clear()
            self._tick_size.clear()
            self._resolved.clear()
            return batch

    def counters(self) -> dict[str, int]:
        with self._lock:
            return {
                "pending_quotes": len(self._quotes),
                "remembered_quotes": len(self._last_quotes),
                "dropped": self.dropped,
                "coalesced": self.coalesced,
                "unchanged_quotes": self.unchanged_quotes,
                "ignored_quotes": self.ignored_quotes,
                "unknown_events": self.unknown_events,
                "market_events": self.market_events,
                "user_events": self.user_events,
            }


def _same_effective_quote(previous: StreamQuote, current: StreamQuote) -> bool:
    if (
        previous.best_bid != current.best_bid
        or previous.best_ask != current.best_ask
        or previous.condition_id != current.condition_id
    ):
        return False
    if current.liquidity is None:
        return True
    return previous.liquidity == current.liquidity


def _has_complete_bbo(quote: StreamQuote) -> bool:
    bid = quote.best_bid
    ask = quote.best_ask
    return bool(
        bid is not None
        and ask is not None
        and Decimal("0") <= bid <= ask <= Decimal("1")
    )


def crypto_ladder_key(row: Mapping[str, Any]) -> CryptoLadderKey:
    """Return a stable event owner, falling back to the settlement contract."""
    event_id = str(row.get("event_id") or "").strip()
    if event_id:
        return ("event", event_id)
    contract = (
        str(row.get("asset") or "").upper(),
        str(row.get("settlement_provider") or row.get("settlement_source") or "").lower(),
        str(row.get("pair") or "").upper(),
        str(row.get("target_time_utc") or ""),
        str(row.get("candle_interval") or "").lower(),
        str(row.get("price_field") or "").lower(),
    )
    if all(contract):
        return ("contract", *contract)
    condition_id = str(row.get("condition_id") or "").strip()
    if condition_id:
        return ("condition", condition_id)
    return ("market", str(row.get("market_id") or "unknown"))


def select_stream_tokens(
    *,
    positions: Sequence[Mapping[str, Any]],
    open_orders: Sequence[Mapping[str, Any]],
    active_market_ids: Sequence[str],
    ranked_candidates: Sequence[Mapping[str, Any]],
    market_rows: Mapping[str, Mapping[str, Any]],
    candidate_group_cap: int = STREAM_CANDIDATE_GROUP_CAP,
) -> DesiredSubscription:
    """Select protected tokens then complete active/candidate crypto ladders."""
    token_to_market: dict[str, str] = {}
    held: set[str] = set()
    open_tokens: set[str] = set()
    active_tokens: set[str] = set()
    candidate_tokens: set[str] = set()

    def token_for(market_id: str, outcome: Any, explicit_token: Any = None) -> str | None:
        row = market_rows.get(market_id)
        if row is None:
            return None
        yes = str(row.get("yes_token_id") or "") or None
        no = str(row.get("no_token_id") or "") or None
        raw = str(explicit_token or outcome or "").strip()
        if not raw:
            return None
        upper = raw.upper()
        if upper in {"YES", "Y", "BUY_YES"}:
            return yes
        if upper in {"NO", "N", "BUY_NO"}:
            return no
        return raw if raw in {yes, no} else None

    for position in positions:
        try:
            size = Decimal(str(position.get("size") or "0"))
        except Exception:
            size = Decimal("0")
        market_id = str(position.get("market_id") or "")
        if not market_id or size == 0:
            continue
        token = token_for(market_id, position.get("outcome"), position.get("token_id"))
        if token:
            held.add(token)
            token_to_market[token] = market_id

    for order in open_orders:
        try:
            size = Decimal(str(order.get("size") or "0"))
        except Exception:
            size = Decimal("0")
        market_id = str(order.get("market_id") or "")
        if not market_id or size <= 0:
            continue
        token = token_for(
            market_id,
            order.get("outcome") or order.get("side"),
            order.get("token_id"),
        )
        if token:
            open_tokens.add(token)
            token_to_market[token] = market_id

    groups: dict[CryptoLadderKey, list[str]] = {}
    market_to_group: dict[str, CryptoLadderKey] = {}
    for market_id, row in market_rows.items():
        key = crypto_ladder_key(row)
        groups.setdefault(key, []).append(market_id)
        market_to_group[market_id] = key

    selected_ladders: list[CryptoLadderKey] = []

    def add_complete_ladder(key: CryptoLadderKey, target: set[str]) -> None:
        if key not in selected_ladders:
            selected_ladders.append(key)
        for market_id in sorted(groups.get(key, [])):
            row = market_rows[market_id]
            for field_name in ("yes_token_id", "no_token_id"):
                token = str(row.get(field_name) or "")
                if token:
                    target.add(token)
                    token_to_market[token] = market_id

    seen_active: set[CryptoLadderKey] = set()
    for market_id in active_market_ids:
        active_key = market_to_group.get(str(market_id))
        if active_key is not None and active_key not in seen_active:
            add_complete_ladder(active_key, active_tokens)
            seen_active.add(active_key)

    selected_candidate_groups = 0
    seen_candidates = set(seen_active)
    for candidate in ranked_candidates:
        market_id = str(candidate.get("market_id") or "")
        candidate_key = market_to_group.get(market_id)
        if candidate_key is None or candidate_key in seen_candidates:
            continue
        if selected_candidate_groups >= max(0, candidate_group_cap):
            break
        add_complete_ladder(candidate_key, candidate_tokens)
        seen_candidates.add(candidate_key)
        selected_candidate_groups += 1

    ordered_tokens = tuple(
        dict.fromkeys(
            [
                *sorted(held),
                *sorted(open_tokens),
                *sorted(active_tokens),
                *sorted(candidate_tokens),
            ]
        )
    )
    return DesiredSubscription(
        token_ids=ordered_tokens,
        token_to_market=dict(token_to_market),
        held_tokens=frozenset(held),
        open_order_tokens=frozenset(open_tokens),
        active_ladder_tokens=frozenset(active_tokens),
        candidate_tokens=frozenset(candidate_tokens),
        selected_ladders=tuple(selected_ladders),
    )


class PolymarketStreamBridge:
    """Official-SDK Market/User Channel host with REST fallback state."""

    def __init__(
        self,
        *,
        private_key: str | None = None,
        funder: str | None = None,
        api_credentials: Any | None = None,
        enable_user_channel: bool = False,
        public_client_factory: Callable[[], Any] | None = None,
        secure_client_factory: Callable[[], Any] | None = None,
        loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
        stale_seconds: float = MARKET_TOKEN_STALE_SECONDS,
        rest_verify_seconds: float = STREAM_REST_VERIFY_SECONDS,
        max_quote_slots: int = STREAM_MAX_QUOTE_SLOTS,
    ) -> None:
        self._private_key = private_key
        self._funder = funder
        self._api_credentials = api_credentials
        self._enable_user = bool(enable_user_channel and private_key and funder)
        self._public_client_factory = public_client_factory
        self._secure_client_factory = secure_client_factory
        self._loop_factory = loop_factory or asyncio.new_event_loop
        self._stale_seconds = max(1.0, float(stale_seconds))
        self._rest_verify_seconds = max(1.0, float(rest_verify_seconds))
        self._queue = _CoalescingQueue(max_quote_slots=max_quote_slots)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = threading.Event()
        self._started = False
        self._desired_tokens: tuple[str, ...] = ()
        self._token_to_market: dict[str, str] = {}
        self._token_last_quote_at: dict[str, float] = {}
        self._token_rest_verified_at: dict[str, float] = {}
        self._rest_backfill_tokens: set[str] = set()
        self._last_market_event_at: float | None = None
        self._last_user_event_at: float | None = None
        self._status: StreamStatus = "disabled"
        self._status_detail = ""
        self._subscription_generation = 0
        self._subscription_inflight_generations: set[int] = set()
        self._active_tokens: set[str] = set()
        self._reader_error_count = 0
        self._client: Any | None = None
        self._handle: Any | None = None
        self._reader_task: asyncio.Task[Any] | None = None
        self._reconnect_task: asyncio.Task[Any] | None = None
        self._secret_values = tuple(
            str(value)
            for value in (private_key, api_credentials)
            if value is not None and str(value)
        )

    @property
    def started(self) -> bool:
        thread = self._thread
        return self._started and thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the bridge thread. Repeated calls are harmless."""
        with self._lock:
            if self.started:
                return
            self._stop_event.clear()
            # A process-local restart is a new transport session. Never reuse
            # pre-stop BBO freshness or REST verification as connection proof.
            self._active_tokens.clear()
            self._rest_backfill_tokens.update(self._desired_tokens)
            for token in self._desired_tokens:
                self._token_last_quote_at.pop(token, None)
                self._token_rest_verified_at.pop(token, None)
            self._set_status("connecting", "starting bridge thread")
            self._thread = threading.Thread(
                target=self._thread_main,
                name="polymarket-stream-bridge",
                daemon=True,
            )
            self._started = True
            self._thread.start()

    def stop(self, *, timeout: float = _SHUTDOWN_JOIN_SECONDS) -> None:
        """Stop the bridge and close SDK resources within a bounded timeout."""
        self._stop_event.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception as exc:
                logger.warning("stream bridge loop stop failed: %s", self._redact(exc))
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        self._close_sync_fallback()
        with self._lock:
            self._started = False
            self._thread = None
            self._loop = None
            self._set_status("disabled", "stopped")

    def set_desired_tokens(
        self,
        token_ids: Sequence[str],
        *,
        token_to_market: Mapping[str, str] | None = None,
    ) -> bool:
        """Set the desired token set without resubscribing on order-only changes."""
        cleaned = tuple(sorted({str(token) for token in token_ids if token}))
        supplied = token_to_market or {}
        mapping = {
            str(token): str(market)
            for token, market in supplied.items()
            if token and market and str(token) in cleaned
        }
        with self._lock:
            if cleaned == self._desired_tokens and mapping == self._token_to_market:
                return False
            self._desired_tokens = cleaned
            self._token_to_market = mapping
            self._subscription_generation += 1
            generation = self._subscription_generation
        loop = self._loop
        if loop is not None and loop.is_running() and not self._stop_event.is_set():
            try:
                asyncio.run_coroutine_threadsafe(self._apply_subscription(generation), loop)
            except Exception as exc:
                self._set_status("degraded", f"resubscribe schedule failed: {self._redact(exc)}")
        return True

    def desired_tokens(self) -> tuple[str, ...]:
        with self._lock:
            return self._desired_tokens

    def token_to_market(self) -> dict[str, str]:
        with self._lock:
            return dict(self._token_to_market)

    def drain(self) -> StreamDrainBatch:
        batch = self._queue.drain()
        with self._lock:
            for token_id, quote in batch.quotes.items():
                if _has_complete_bbo(quote):
                    self._token_last_quote_at[token_id] = max(
                        quote.received_at,
                        self._token_last_quote_at.get(token_id, quote.received_at),
                    )
                    self._last_market_event_at = max(
                        quote.received_at,
                        self._last_market_event_at or quote.received_at,
                    )
            if batch.user_hints:
                self._last_user_event_at = batch.user_hints[-1].received_at
            if batch.quotes and self._status in {"connecting", "degraded", "stale"}:
                self._set_status("live", "receiving market quotes")
            self._refresh_stale_status(time.monotonic())
        return batch

    def health(self) -> StreamHealth:
        counters = self._queue.counters()
        now = time.monotonic()
        with self._lock:
            self._refresh_stale_status(now)
            status = self._status
            desired = self._desired_tokens
            detail = {
                "subscribed_token_count": len(desired),
                "market_last_event_at": self._last_market_event_at,
                "user_last_event_at": self._last_user_event_at,
                **counters,
                "rest_fallback_active": self.rest_fallback_active(now=now),
                "user_channel_enabled": self._enable_user,
                "reader_errors": self._reader_error_count,
                "rest_backfill_pending_count": len(self._rest_backfill_tokens),
                "rest_verification_due_count": sum(
                    1 for token in desired if self.needs_rest_verification(token, now=now)
                ),
                "detail": self._status_detail,
            }
        return StreamHealth(status=status, updated_at=time.time(), detail=detail)

    def is_token_fresh(self, token_id: str, *, now: float | None = None) -> bool:
        mono = time.monotonic() if now is None else now
        token = str(token_id)
        with self._lock:
            if token in self._rest_backfill_tokens:
                return False
            last = self._token_last_quote_at.get(token)
        return last is not None and (mono - last) <= self._stale_seconds

    def needs_rest_backfill(self, token_id: str) -> bool:
        with self._lock:
            return str(token_id) in self._rest_backfill_tokens

    def needs_rest_verification(self, token_id: str, *, now: float | None = None) -> bool:
        mono = time.monotonic() if now is None else now
        token = str(token_id)
        with self._lock:
            if token in self._rest_backfill_tokens:
                return True
            verified = self._token_rest_verified_at.get(token)
        return verified is None or (mono - verified) >= self._rest_verify_seconds

    def mark_rest_verified(self, token_id: str, *, now: float | None = None) -> None:
        token = str(token_id)
        with self._lock:
            self._rest_backfill_tokens.discard(token)
            self._token_rest_verified_at[token] = time.monotonic() if now is None else now

    def subscription_generation(self) -> int:
        with self._lock:
            return self._subscription_generation

    def rest_fallback_active(self, *, now: float | None = None) -> bool:
        mono = time.monotonic() if now is None else now
        with self._lock:
            status = self._status
            desired = self._desired_tokens
        if status != "live" or not desired:
            return True
        return any(
            not self.is_token_fresh(token, now=mono)
            or self.needs_rest_verification(token, now=mono)
            for token in desired
        )

    def _set_status(self, status: StreamStatus, detail: str = "") -> None:
        self._status = status
        self._status_detail = self._redact(detail)[:240]

    def _redact(self, value: Any) -> str:
        text = str(value)
        for secret in self._secret_values:
            text = text.replace(secret, "[redacted]")
        return re.sub(
            r"(?i)(private[_ -]?key|api[_ -]?key|secret|password|credential)(\s*[:=]\s*)\S+",
            r"\1\2[redacted]",
            text,
        )

    def _refresh_stale_status(self, now: float) -> None:
        if self._status != "live" or not self._desired_tokens:
            return
        seen = [token for token in self._desired_tokens if token in self._token_last_quote_at]
        if seen and all(
            now - self._token_last_quote_at[token] > self._stale_seconds for token in seen
        ):
            self._set_status("stale", f"all {len(seen)} seen tokens past BBO TTL")

    def _thread_main(self) -> None:
        loop = self._loop_factory()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.create_task(self._async_main())
            loop.run_forever()
        except Exception as exc:
            logger.warning("stream bridge loop failed: %s", self._redact(exc))
            self._set_status("degraded", f"loop failed: {self._redact(exc)}")
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _async_main(self) -> None:
        try:
            await self._open_client()
            await self._apply_subscription(self.subscription_generation())
            if self._status == "connecting":
                self._set_status("live", "bridge ready")
            while not self._stop_event.is_set():
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("stream bridge startup failed: %s", self._redact(exc))
            self._set_status("degraded", f"startup failed: {self._redact(exc)}")
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            await self._async_shutdown()

    async def _open_client(self) -> None:
        if self._client is not None:
            return
        if self._enable_user:
            if self._secure_client_factory is not None:
                self._client = self._secure_client_factory()
            else:
                from polymarket import AsyncSecureClient

                kwargs: dict[str, Any] = {
                    "private_key": self._private_key,
                    "wallet": self._funder,
                }
                if self._api_credentials is not None:
                    kwargs["credentials"] = self._api_credentials
                self._client = await AsyncSecureClient.create(**kwargs)
                self._api_credentials = None
        elif self._public_client_factory is not None:
            self._client = self._public_client_factory()
        else:
            from polymarket import AsyncPublicClient

            self._client = AsyncPublicClient()
        self._set_status("connecting", "client open")

    async def _apply_subscription(self, generation: int) -> None:
        if self._stop_event.is_set() or generation != self.subscription_generation():
            return
        with self._lock:
            if generation in self._subscription_inflight_generations:
                return
            self._subscription_inflight_generations.add(generation)
        try:
            await self._replace_subscription(generation)
        finally:
            with self._lock:
                self._subscription_inflight_generations.discard(generation)

    async def _replace_subscription(self, generation: int) -> None:
        with self._lock:
            tokens = self._desired_tokens
        try:
            await self._open_client()
            specs: list[Any] = []
            if tokens:
                from polymarket.streams import MarketSpec

                specs.append(MarketSpec(token_ids=list(tokens), custom_feature_enabled=True))
            if self._enable_user:
                from polymarket.streams import UserSpec

                specs.append(UserSpec())
            if not specs:
                await self._close_handle()
                self._activate_subscription_tokens(())
                self._set_status("live", "no tokens subscribed; REST fallback")
                return
            if self._client is None:
                raise RuntimeError("stream client unavailable")
            subscription = self._client.subscribe(specs if len(specs) > 1 else specs[0])
            new_handle = await subscription if inspect.isawaitable(subscription) else subscription
            if self._stop_event.is_set() or generation != self.subscription_generation():
                await self._close_subscription_handle(new_handle)
                return
            old_handle = self._handle
            old_task = self._reader_task
            self._handle = new_handle
            self._reader_task = asyncio.create_task(
                self._read_handle(new_handle, generation),
                name="polymarket-stream-reader",
            )
            if old_task is not None and old_task is not asyncio.current_task():
                old_task.cancel()
                try:
                    await old_task
                except (asyncio.CancelledError, Exception):
                    pass
            if old_handle is not None:
                await self._close_subscription_handle(old_handle)
            self._activate_subscription_tokens(tokens)
            self._set_status("live", f"subscribed tokens={len(tokens)} user={self._enable_user}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reader_error_count += 1
            self._set_status("degraded", f"subscribe failed: {self._redact(exc)}")
            self._request_reconnect(generation)

    def _activate_subscription_tokens(self, tokens: Sequence[str]) -> None:
        active = {str(token) for token in tokens if token}
        with self._lock:
            added = active - self._active_tokens
            removed = self._active_tokens - active
            self._rest_backfill_tokens.intersection_update(active)
            self._rest_backfill_tokens.update(added)
            for token in added | removed:
                self._token_last_quote_at.pop(token, None)
                self._token_rest_verified_at.pop(token, None)
            self._active_tokens = active

    async def _read_handle(self, handle: Any, generation: int) -> None:
        disconnected = False
        try:
            async for event in handle:
                if self._stop_event.is_set() or generation != self.subscription_generation():
                    return
                try:
                    items = normalize_sdk_events(event)
                except Exception:
                    self._queue.publish(None)
                    continue
                if not items:
                    self._queue.publish(None)
                    continue
                desired = set(self.desired_tokens())
                latest_by_token: dict[str, StreamQuote] = {}
                other: list[NormalizedStreamEvent] = []
                for normalized in items:
                    if isinstance(normalized, StreamQuote):
                        if normalized.token_id not in desired:
                            self._queue.ignore_quote()
                            continue
                        latest_by_token[normalized.token_id] = normalized
                    else:
                        other.append(normalized)
                for normalized in [*latest_by_token.values(), *other]:
                    self._queue.publish(normalized)
                    with self._lock:
                        if isinstance(normalized, StreamQuote) and _has_complete_bbo(normalized):
                            self._last_market_event_at = normalized.received_at
                            self._token_last_quote_at[normalized.token_id] = normalized.received_at
                        elif isinstance(normalized, StreamUserHint):
                            self._last_user_event_at = normalized.received_at
            disconnected = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            disconnected = True
            logger.warning("stream reader stopped: %s", self._redact(exc))
        finally:
            if (
                disconnected
                and not self._stop_event.is_set()
                and generation == self.subscription_generation()
            ):
                with self._lock:
                    self._reader_error_count += 1
                    self._rest_backfill_tokens.update(self._desired_tokens)
                self._set_status("degraded", "reader disconnected; REST fallback")
                self._request_reconnect(generation)

    def _request_reconnect(self, generation: int) -> None:
        if self._stop_event.is_set():
            return
        task = self._reconnect_task
        if task is not None and not task.done():
            return
        try:
            self._reconnect_task = asyncio.create_task(
                self._reconnect_after_delay(generation),
                name="polymarket-stream-reconnect",
            )
        except RuntimeError:
            pass

    async def _reconnect_after_delay(self, generation: int) -> None:
        await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
        if not self._stop_event.is_set() and generation == self.subscription_generation():
            await self._replace_subscription(generation)

    async def _close_handle(self) -> None:
        task = self._reader_task
        handle = self._handle
        self._reader_task = None
        self._handle = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close_subscription_handle(handle)

    @staticmethod
    async def _close_subscription_handle(handle: Any | None) -> None:
        if handle is None:
            return
        try:
            close = getattr(handle, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        except Exception:
            pass

    async def _async_shutdown(self) -> None:
        reconnect = self._reconnect_task
        self._reconnect_task = None
        if reconnect is not None and reconnect is not asyncio.current_task():
            reconnect.cancel()
        await self._close_handle()
        client = self._client
        self._client = None
        if client is not None:
            close = getattr(client, "close", None) or getattr(client, "aclose", None)
            if close is not None:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass

    def _close_sync_fallback(self) -> None:
        handle = self._handle
        client = self._client
        self._handle = None
        self._client = None
        self._reader_task = None
        for resource in (handle, client):
            if resource is None:
                continue
            try:
                close = getattr(resource, "close", None) or getattr(resource, "aclose", None)
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        result.close()
            except Exception:
                pass
