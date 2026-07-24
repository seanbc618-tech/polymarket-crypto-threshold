"""Settlement-aligned Binance price stream over the official public SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlsplit

BINANCE_STREAM_SOURCE_VERSION = "binance-spot-sdk-stream-v1"
BINANCE_STREAM_URL = "wss://stream.binance.com:443"
DEFAULT_PAIRS = ("BTCUSDT", "ETHUSDT")


@dataclass(frozen=True)
class ReferencePriceTick:
    provider: str
    pair: str
    candle_interval: str
    price_field: str
    price: Decimal
    provider_timestamp: datetime
    received_at: datetime
    fresh: bool
    source_version: str = BINANCE_STREAM_SOURCE_VERSION
    sequence: str | None = None
    payload_hash: str | None = None


class ReferencePriceStream(Protocol):
    """Normalization-only interface; strategy and persistence stay elsewhere."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def drain(self) -> tuple[ReferencePriceTick, ...]: ...

    def health(self) -> Mapping[str, object]: ...


class BinanceReferencePriceStream:
    """Coalesce closed 1m Close ticks; never runs strategy or writes SQLite."""

    def __init__(
        self,
        *,
        pairs: tuple[str, ...] = DEFAULT_PAIRS,
        stale_seconds: float = 45.0,
        max_tick_slots: int = 16,
        stream_url: str = BINANCE_STREAM_URL,
        proxy_url: str | None = None,
        connection_factory: Callable[[], Awaitable[Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if stale_seconds <= 0 or max_tick_slots < 1:
            raise ValueError("invalid Binance stream bounds")
        self._pairs = tuple(sorted({pair.upper() for pair in pairs}))
        self._stale_seconds = stale_seconds
        self._max_tick_slots = max_tick_slots
        self._stream_url = stream_url.rstrip("/")
        self._proxy = _sdk_proxy_config(proxy_url)
        self._connection_factory = connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._ticks: OrderedDict[str, ReferencePriceTick] = OrderedDict()
        self._latest_ticks: dict[str, ReferencePriceTick] = {}
        self._status = "disabled"
        self._detail: dict[str, object] = {}
        self._dropped = 0
        self._coalesced = 0
        self._generation = 0
        self._last_error: str | None = None
        self._last_event_monotonic: float | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._status = "starting"
            self._thread = threading.Thread(
                target=self._thread_main,
                name="binance-reference-price-stream",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            loop = self._loop
            run_task = self._run_task
            if thread is None:
                self._status = "stopped"
                return
            self._stop.set()
        if loop is not None and run_task is not None and loop.is_running():
            loop.call_soon_threadsafe(run_task.cancel)
        thread.join(timeout=5)
        with self._lock:
            if thread.is_alive():
                self._status = "stop_timeout"
            else:
                self._thread = None
                self._status = "stopped"

    def drain(self) -> tuple[ReferencePriceTick, ...]:
        with self._lock:
            ticks = tuple(self._ticks.values())
            self._ticks.clear()
            return ticks

    def is_pair_fresh(self, pair: str) -> bool:
        with self._lock:
            tick = self._latest_ticks.get(pair.upper())
        if tick is None or not tick.fresh:
            return False
        age = (_utc(self._clock()) - tick.received_at).total_seconds()
        return -5 <= age <= self._stale_seconds

    def health(self) -> Mapping[str, object]:
        with self._lock:
            now = _utc(self._clock())
            status = self._status
            detail = {
                **self._detail,
                "pairs": list(self._pairs),
                "fresh_pairs": [
                    pair
                    for pair in self._pairs
                    if pair in self._latest_ticks
                    and self._latest_ticks[pair].fresh
                    and -5
                    <= (now - self._latest_ticks[pair].received_at).total_seconds()
                    <= self._stale_seconds
                ],
                "queued": len(self._ticks),
                "dropped": self._dropped,
                "coalesced": self._coalesced,
                "generation": self._generation,
                "source_version": BINANCE_STREAM_SOURCE_VERSION,
                "proxy_enabled": self._proxy is not None,
                "last_error": self._last_error,
            }
        return {"status": status, "detail": _scrub(detail)}

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._set_health("failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._loop = None
                self._run_task = None

    async def _run(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._run_task = asyncio.current_task()
        while not self._stop.is_set():
            connection: Any = None
            handles: list[Any] = []
            with self._lock:
                self._last_event_monotonic = None
            self._set_health("connecting")
            try:
                connection = await self._create_connection()
                if connection is None:
                    raise RuntimeError("official Binance SDK returned no connection")
                interval = self._one_minute_interval()
                for pair in self._pairs:
                    handle = await connection.kline(
                        symbol=pair.lower(), interval=interval
                    )
                    handle.on("message", self._on_message)
                    handles.append(handle)
                with self._lock:
                    self._generation += 1
                subscribed_at = time.monotonic()
                self._set_health("connected")
                while not self._stop.is_set():
                    await asyncio.sleep(0.1)
                    with self._lock:
                        last_event = self._last_event_monotonic
                    if (
                        time.monotonic() - (last_event or subscribed_at)
                        > self._stale_seconds
                    ):
                        raise TimeoutError("Binance stream event timeout")
            except Exception as exc:
                self._set_health("degraded", error=f"{type(exc).__name__}: {exc}")
            finally:
                for handle in handles:
                    try:
                        await handle.unsubscribe()
                    except Exception:
                        pass
                if connection is not None:
                    try:
                        await connection.close_connection(close_session=True)
                    except Exception:
                        pass
            if not self._stop.is_set():
                await asyncio.sleep(0.5)

    async def _create_connection(self) -> Any:
        if self._connection_factory is not None:
            return await self._connection_factory()
        from binance_common.configuration import ConfigurationWebSocketStreams
        from binance_common.constants import SPOT_WS_STREAMS_PROD_URL
        from binance_sdk_spot.spot import Spot

        client = Spot(
            config_ws_streams=ConfigurationWebSocketStreams(
                stream_url=self._stream_url or SPOT_WS_STREAMS_PROD_URL,
                proxy=self._proxy,
            )
        )
        streams = client.websocket_streams
        try:
            connection = await streams.create_connection()
            if connection is None:
                raise RuntimeError("official Binance SDK returned no connection")
            return connection
        except BaseException:
            await streams.close_connection(close_session=True)
            raise

    @staticmethod
    def _one_minute_interval() -> Any:
        from binance_sdk_spot.websocket_streams.models.enums import KlineIntervalEnum

        # SDK 10.0.0 interpolates this value directly into the stream name.
        return KlineIntervalEnum.INTERVAL_1m.value

    def _on_message(self, event: Any) -> None:
        tick = normalize_binance_kline(
            event,
            received_at=_utc(self._clock()),
            max_age_seconds=self._stale_seconds,
        )
        with self._lock:
            self._last_event_monotonic = time.monotonic()
            if tick is None or tick.pair not in self._pairs:
                return
            self._latest_ticks[tick.pair] = tick
            if tick.pair in self._ticks:
                self._coalesced += 1
                del self._ticks[tick.pair]
            elif len(self._ticks) >= self._max_tick_slots:
                self._ticks.popitem(last=False)
                self._dropped += 1
            self._ticks[tick.pair] = tick

    def _set_health(self, status: str, **detail: object) -> None:
        with self._lock:
            self._status = status
            self._detail = detail
            if "error" in detail:
                self._last_error = str(detail["error"])


def normalize_binance_kline(
    event: Any,
    *,
    received_at: datetime,
    max_age_seconds: float = 45.0,
) -> ReferencePriceTick | None:
    """Normalize only closed UTC 1m Close events from BTC/ETH USDT."""
    payload = _mapping(event)
    kline = _mapping(payload.get("k"))
    if not kline or kline.get("x") is not True:
        return None
    pair = str(payload.get("s") or kline.get("s") or "").upper()
    if pair not in DEFAULT_PAIRS or str(kline.get("i") or "") != "1m":
        return None
    close = kline.get("c")
    close_time = kline.get("T")
    if close is None or close_time is None:
        return None
    provider_timestamp = datetime.fromtimestamp(int(close_time) / 1000, tz=UTC)
    age = (_utc(received_at) - _utc(provider_timestamp)).total_seconds()
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    event_time = payload.get("E")
    last_trade_id = kline.get("L")
    sequence = ":".join(str(value) for value in (event_time, last_trade_id) if value is not None)
    return ReferencePriceTick(
        provider="binance",
        pair=pair,
        candle_interval="1m",
        price_field="Close",
        price=Decimal(str(close)),
        provider_timestamp=_utc(provider_timestamp),
        received_at=_utc(received_at),
        fresh=-5 <= age <= max_age_seconds,
        sequence=sequence or None,
        payload_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def _sdk_proxy_config(
    proxy_url: str | None,
) -> dict[str, str | int | dict[str, str]] | None:
    """Translate an exact no-auth proxy origin to the official SDK shape."""
    if proxy_url is None or not proxy_url.strip():
        return None
    parsed = urlsplit(proxy_url.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid Binance stream proxy port") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or port is None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("invalid unauthenticated Binance stream proxy URL")
    return {
        "protocol": parsed.scheme.lower(),
        "host": parsed.hostname,
        "port": port,
    }


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    if hasattr(value, "model_dump"):
        result = value.model_dump(by_alias=True, exclude_none=True)
        return dict(result) if isinstance(result, Mapping) else {}
    return {
        key: getattr(value, key)
        for key in ("e", "E", "s", "k")
        if getattr(value, key, None) is not None
    }


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
