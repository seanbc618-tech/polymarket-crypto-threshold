"""Chainlink reference prices over the official Polymarket public SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from crypto_threshold.adapters.prices.stream import ReferencePriceTick
from crypto_threshold.domain.assets import SUPPORTED_CHAINLINK_PAIRS

CHAINLINK_STREAM_SOURCE_VERSION = "polymarket-chainlink-rtds-sdk-v1"
DEFAULT_CHAINLINK_PAIRS = tuple(sorted(SUPPORTED_CHAINLINK_PAIRS))

SubscriptionFactory = Callable[
    [tuple[str, ...]],
    Awaitable[tuple[Any, Any]],
]


class ChainlinkReferencePriceStream:
    """Keep a bounded in-memory tick window; never runs strategy or writes SQL."""

    def __init__(
        self,
        *,
        pairs: tuple[str, ...] = DEFAULT_CHAINLINK_PAIRS,
        stale_seconds: float = 5.0,
        history_seconds: float = 1_200.0,
        max_ticks_per_pair: int = 2_000,
        subscription_factory: SubscriptionFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized = tuple(sorted({pair.upper() for pair in pairs}))
        if (
            not normalized
            or any(pair not in SUPPORTED_CHAINLINK_PAIRS for pair in normalized)
            or stale_seconds <= 0
            or history_seconds <= 0
            or max_ticks_per_pair < 2
        ):
            raise ValueError("invalid Chainlink stream configuration")
        self._pairs = normalized
        self._stale_seconds = stale_seconds
        self._history_seconds = history_seconds
        self._max_ticks_per_pair = max_ticks_per_pair
        self._subscription_factory = subscription_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._history: dict[str, deque[ReferencePriceTick]] = {
            pair: deque(maxlen=max_ticks_per_pair) for pair in self._pairs
        }
        self._latest: dict[str, ReferencePriceTick] = {}
        self._pending: OrderedDict[str, ReferencePriceTick] = OrderedDict()
        self._status = "disabled"
        self._generation = 0
        self._coalesced = 0
        self._dropped = 0
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._status = "starting"
            self._thread = threading.Thread(
                target=self._thread_main,
                name="chainlink-reference-price-stream",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            loop = self._loop
            task = self._run_task
            if thread is None:
                self._status = "stopped"
                return
            self._stop.set()
        if loop is not None and task is not None and loop.is_running():
            loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout=5)
        with self._lock:
            if thread.is_alive():
                self._status = "stop_timeout"
            else:
                self._thread = None
                self._status = "stopped"

    def drain(self) -> tuple[ReferencePriceTick, ...]:
        """Return one latest tick per pair for health evidence."""
        with self._lock:
            ticks = tuple(self._pending.values())
            self._pending.clear()
            return ticks

    def history(
        self,
        pair: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[ReferencePriceTick, ...]:
        normalized = pair.upper()
        with self._lock:
            ticks = tuple(self._history.get(normalized, ()))
        start_utc = _utc(start) if start is not None else None
        end_utc = _utc(end) if end is not None else None
        return tuple(
            tick
            for tick in ticks
            if (start_utc is None or tick.provider_timestamp >= start_utc)
            and (end_utc is None or tick.provider_timestamp <= end_utc)
        )

    def boundary_tick(
        self,
        pair: str,
        boundary: datetime,
        *,
        tolerance_seconds: float = 2.0,
    ) -> ReferencePriceTick | None:
        boundary = _utc(boundary)
        candidates = self.history(
            pair,
            start=boundary,
            end=boundary + timedelta(seconds=tolerance_seconds),
        )
        return min(candidates, key=lambda tick: tick.provider_timestamp, default=None)

    def latest_tick(
        self,
        pair: str,
        *,
        at: datetime | None = None,
        max_age_seconds: float | None = None,
    ) -> ReferencePriceTick | None:
        normalized = pair.upper()
        with self._lock:
            tick = self._latest.get(normalized)
        if tick is None:
            return None
        now = _utc(at or self._clock())
        max_age = self._stale_seconds if max_age_seconds is None else max_age_seconds
        age = (now - tick.provider_timestamp).total_seconds()
        return tick if tick.fresh and -2 <= age <= max_age else None

    def health(self) -> Mapping[str, object]:
        with self._lock:
            now = _utc(self._clock())
            fresh_pairs = [
                pair
                for pair, tick in self._latest.items()
                if tick.fresh
                and -2
                <= (now - tick.provider_timestamp).total_seconds()
                <= self._stale_seconds
            ]
            detail = {
                "pairs": list(self._pairs),
                "fresh_pairs": sorted(fresh_pairs),
                "history_counts": {
                    pair: len(self._history[pair]) for pair in self._pairs
                },
                "queued": len(self._pending),
                "coalesced": self._coalesced,
                "dropped": self._dropped,
                "generation": self._generation,
                "source_version": CHAINLINK_STREAM_SOURCE_VERSION,
                "last_error": self._last_error,
            }
            return {"status": self._status, "detail": _scrub(detail)}

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._set_health("failed", type(exc).__name__)
        finally:
            with self._lock:
                self._loop = None
                self._run_task = None

    async def _run(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._run_task = asyncio.current_task()
        while not self._stop.is_set():
            client: Any = None
            handle: Any = None
            self._set_health("connecting")
            try:
                client, handle = await self._open_subscription()
                with self._lock:
                    self._generation += 1
                self._set_health("connected")
                while not self._stop.is_set():
                    event = await asyncio.wait_for(
                        handle.__anext__(),
                        timeout=self._stale_seconds,
                    )
                    tick = normalize_chainlink_price(
                        event,
                        received_at=_utc(self._clock()),
                        max_age_seconds=self._stale_seconds,
                    )
                    if tick is not None and tick.pair in self._pairs:
                        self._store_tick(tick)
            except (StopAsyncIteration, asyncio.CancelledError):
                if self._stop.is_set():
                    break
                self._set_health("degraded", "Chainlink subscription ended")
            except Exception as exc:
                self._set_health("degraded", type(exc).__name__)
            finally:
                if handle is not None:
                    try:
                        await handle.close()
                    except Exception:
                        pass
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass
            if not self._stop.is_set():
                await asyncio.sleep(0.5)

    async def _open_subscription(self) -> tuple[Any, Any]:
        if self._subscription_factory is not None:
            return await self._subscription_factory(self._pairs)
        from polymarket import AsyncPublicClient
        from polymarket.streams import CryptoPricesSpec

        client = AsyncPublicClient()
        try:
            handle = await client.subscribe(
                CryptoPricesSpec(
                    topic="prices.crypto.chainlink",
                    symbols=[pair.lower() for pair in self._pairs],
                )
            )
            return client, handle
        except BaseException:
            await client.close()
            raise

    def _store_tick(self, tick: ReferencePriceTick) -> None:
        with self._lock:
            history = self._history[tick.pair]
            if history and history[-1].provider_timestamp == tick.provider_timestamp:
                history[-1] = tick
                self._coalesced += 1
            else:
                if len(history) == history.maxlen:
                    self._dropped += 1
                history.append(tick)
            cutoff = tick.provider_timestamp - timedelta(seconds=self._history_seconds)
            while history and history[0].provider_timestamp < cutoff:
                history.popleft()
            self._latest[tick.pair] = tick
            if tick.pair in self._pending:
                self._coalesced += 1
                del self._pending[tick.pair]
            self._pending[tick.pair] = tick

    def _set_health(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._status = status
            if error is not None:
                self._last_error = error


def normalize_chainlink_price(
    event: Any,
    *,
    received_at: datetime,
    max_age_seconds: float = 5.0,
) -> ReferencePriceTick | None:
    payload = _mapping(event)
    topic = str(payload.get("topic") or getattr(event, "topic", ""))
    if topic not in {"prices.crypto.chainlink", "crypto_prices_chainlink"}:
        return None
    body = _mapping(payload.get("payload") or getattr(event, "payload", None))
    pair = str(body.get("symbol") or "").upper()
    if pair not in SUPPORTED_CHAINLINK_PAIRS:
        return None
    timestamp = _epoch_ms(body.get("timestamp"))
    if timestamp is None:
        return None
    try:
        price = Decimal(str(body.get("value")))
    except (InvalidOperation, ValueError):
        return None
    if price <= 0:
        return None
    received_at = _utc(received_at)
    age = (received_at - timestamp).total_seconds()
    raw = {
        "topic": topic,
        "type": payload.get("type") or getattr(event, "type", None),
        "timestamp": _json_value(payload.get("timestamp") or getattr(event, "timestamp", None)),
        "payload": {
            "symbol": pair.lower(),
            "timestamp": int(timestamp.timestamp() * 1000),
            "value": str(price),
        },
    }
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return ReferencePriceTick(
        provider="chainlink",
        pair=pair,
        candle_interval="tick",
        price_field="value",
        price=price,
        provider_timestamp=timestamp,
        received_at=received_at,
        fresh=-2 <= age <= max_age_seconds,
        source_version=CHAINLINK_STREAM_SOURCE_VERSION,
        sequence=str(int(timestamp.timestamp() * 1000)),
        payload_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        raw_payload=raw,
    )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        result = value.model_dump(by_alias=True, exclude_none=True)
        return dict(result) if isinstance(result, Mapping) else {}
    return {}


def _epoch_ms(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _scrub(value: Any) -> Any:
    secret_fragments = ("key", "secret", "credential", "private", "auth", "password")
    if isinstance(value, Mapping):
        return {
            str(key): _scrub(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in secret_fragments)
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
