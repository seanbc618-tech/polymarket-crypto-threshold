"""Official-SDK Binance spot depth/trade stream plus public REST snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from crypto_threshold.adapters.prices.stream import _sdk_proxy_config
from crypto_threshold.domain.microstructure import L2Level, TradeAggressor
from crypto_threshold.domain.microstructure_capture import (
    PerpetualMark,
    RawMicrostructureEvent,
    RawMicrostructureKind,
)

BINANCE_MICROSTRUCTURE_SOURCE_VERSION = "binance-spot-depth-trade-sdk-v1"
BINANCE_DEPTH_SNAPSHOT_SOURCE_VERSION = "binance-spot-depth-rest-v1"
BINANCE_FUTURES_MARK_SOURCE_VERSION = "binance-usdm-premium-index-v1"
DEFAULT_MICROSTRUCTURE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")


class BinanceMicrostructureRestClient:
    """Fetch public spot depth snapshots and USD-M perpetual mark prices."""

    def __init__(
        self,
        *,
        spot_base_url: str = "https://api.binance.com/api/v3",
        futures_base_url: str = "https://fapi.binance.com/fapi/v1",
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.spot_base_url = spot_base_url.rstrip("/")
        self.futures_base_url = futures_base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=10)
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def depth_snapshot(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> RawMicrostructureEvent:
        normalized_symbol = _symbol(symbol)
        if limit not in {5, 10, 20, 50, 100, 500, 1000, 5000}:
            raise ValueError("unsupported Binance depth snapshot limit")
        response = self._client.get(
            f"{self.spot_base_url}/depth",
            params={"symbol": normalized_symbol, "limit": limit},
        )
        response.raise_for_status()
        payload = _mapping(response.json())
        update_id = _positive_int(payload.get("lastUpdateId"), "lastUpdateId")
        bids = _levels(payload.get("bids"), allow_zero=False)
        asks = _levels(payload.get("asks"), allow_zero=False)
        if not bids or not asks or max(level.price for level in bids) >= min(
            level.price for level in asks
        ):
            raise ValueError("Binance depth snapshot is empty or crossed")
        received_at = _utc(self._clock())
        return RawMicrostructureEvent(
            symbol=normalized_symbol,
            kind=RawMicrostructureKind.SNAPSHOT,
            exchange_at=received_at,
            received_at=received_at,
            source="binance_spot_rest",
            source_version=BINANCE_DEPTH_SNAPSHOT_SOURCE_VERSION,
            payload_hash=_payload_hash(payload),
            raw_payload=payload,
            venue_sequence_start=update_id,
            venue_sequence_end=update_id,
            bids=bids,
            asks=asks,
            timestamp_trusted=False,
        )

    def perpetual_mark(self, symbol: str) -> PerpetualMark:
        normalized_symbol = _symbol(symbol)
        response = self._client.get(
            f"{self.futures_base_url}/premiumIndex",
            params={"symbol": normalized_symbol},
        )
        response.raise_for_status()
        payload = _mapping(response.json())
        if str(payload.get("symbol") or "").upper() != normalized_symbol:
            raise ValueError("Binance perpetual mark symbol mismatch")
        mark = Decimal(str(payload.get("markPrice")))
        index = Decimal(str(payload.get("indexPrice")))
        if mark <= 0 or index <= 0:
            raise ValueError("Binance perpetual mark prices must be positive")
        event_ms = _positive_int(payload.get("time"), "time")
        received_at = _utc(self._clock())
        return PerpetualMark(
            symbol=normalized_symbol,
            mark_price=mark,
            index_price=index,
            funding_rate=(
                Decimal(str(payload["lastFundingRate"]))
                if payload.get("lastFundingRate") is not None
                else None
            ),
            exchange_at=datetime.fromtimestamp(event_ms / 1000, tz=UTC),
            received_at=received_at,
            payload_hash=_payload_hash(payload),
            raw_payload=payload,
        )


class BinanceMicrostructureStream:
    """Bounded public depth/trade stream; it never signs or submits anything."""

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = DEFAULT_MICROSTRUCTURE_SYMBOLS,
        stale_seconds: float = 30,
        max_events: int = 200_000,
        stream_url: str = "wss://stream.binance.com:443",
        proxy_url: str | None = None,
        connection_factory: Callable[[], Awaitable[Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if stale_seconds <= 0 or max_events < 1_000:
            raise ValueError("invalid Binance microstructure stream bounds")
        self._symbols = tuple(sorted({_symbol(symbol) for symbol in symbols}))
        if not self._symbols:
            raise ValueError("at least one Binance microstructure symbol is required")
        self._stale_seconds = stale_seconds
        self._max_events = max_events
        self._stream_url = stream_url.rstrip("/")
        self._proxy = _sdk_proxy_config(proxy_url)
        self._connection_factory = connection_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._events: deque[RawMicrostructureEvent] = deque()
        self._status = "disabled"
        self._generation = 0
        self._dropped = 0
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
                name="binance-microstructure-stream",
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

    def drain(self, *, limit: int = 50_000) -> tuple[RawMicrostructureEvent, ...]:
        if limit < 1:
            raise ValueError("drain limit must be positive")
        with self._lock:
            count = min(limit, len(self._events))
            return tuple(self._events.popleft() for _ in range(count))

    def health(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "status": self._status,
                "detail": {
                    "symbols": list(self._symbols),
                    "queued": len(self._events),
                    "dropped": self._dropped,
                    "generation": self._generation,
                    "source_version": BINANCE_MICROSTRUCTURE_SOURCE_VERSION,
                    "proxy_enabled": self._proxy is not None,
                    "last_error": self._last_error,
                },
            }

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._set_health("failed", f"{type(exc).__name__}: {exc}")
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
                for symbol in self._symbols:
                    depth = await connection.diff_book_depth(
                        symbol=symbol.lower(),
                        update_speed=self._depth_speed(),
                    )
                    depth.on("message", self._on_depth)
                    handles.append(depth)
                    trade = await connection.agg_trade(symbol=symbol.lower())
                    trade.on("message", self._on_trade)
                    handles.append(trade)
                with self._lock:
                    self._generation += 1
                subscribed_at = time.monotonic()
                self._set_health("connected")
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)
                    with self._lock:
                        last_event = self._last_event_monotonic
                    if (
                        time.monotonic() - (last_event or subscribed_at)
                        > self._stale_seconds
                    ):
                        raise TimeoutError("Binance microstructure stream timeout")
            except Exception as exc:
                self._set_health("degraded", f"{type(exc).__name__}: {exc}")
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
    def _depth_speed() -> Any:
        from binance_sdk_spot.websocket_streams.models.enums import (
            DiffBookDepthUpdateSpeedEnum,
        )

        return DiffBookDepthUpdateSpeedEnum.UPDATE_SPEED_100ms.value

    def _on_depth(self, event: Any) -> None:
        normalized = normalize_binance_depth(
            event,
            received_at=_utc(self._clock()),
        )
        self._publish(normalized)

    def _on_trade(self, event: Any) -> None:
        normalized = normalize_binance_agg_trade(
            event,
            received_at=_utc(self._clock()),
        )
        self._publish(normalized)

    def _publish(self, event: RawMicrostructureEvent) -> None:
        with self._lock:
            self._last_event_monotonic = time.monotonic()
            if event.symbol not in self._symbols:
                return
            if len(self._events) >= self._max_events:
                self._events.popleft()
                self._dropped += 1
                self._status = "overflow"
            self._events.append(event)

    def _set_health(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._status = status
            if error is not None:
                self._last_error = error


def normalize_binance_depth(
    event: Any,
    *,
    received_at: datetime,
) -> RawMicrostructureEvent:
    payload = _mapping(event)
    if str(payload.get("e") or "") != "depthUpdate":
        raise ValueError("unexpected Binance depth event type")
    symbol = _symbol(str(payload.get("s") or ""))
    event_ms = _positive_int(payload.get("E"), "E")
    first = _positive_int(payload.get("U"), "U")
    final = _positive_int(payload.get("u"), "u")
    if first > final:
        raise ValueError("Binance depth sequence is reversed")
    return RawMicrostructureEvent(
        symbol=symbol,
        kind=RawMicrostructureKind.DEPTH,
        exchange_at=datetime.fromtimestamp(event_ms / 1000, tz=UTC),
        received_at=_utc(received_at),
        source="binance_spot_websocket",
        source_version=BINANCE_MICROSTRUCTURE_SOURCE_VERSION,
        payload_hash=_payload_hash(payload),
        raw_payload=payload,
        venue_sequence_start=first,
        venue_sequence_end=final,
        bids=_levels(payload.get("b"), allow_zero=True),
        asks=_levels(payload.get("a"), allow_zero=True),
    )


def normalize_binance_agg_trade(
    event: Any,
    *,
    received_at: datetime,
) -> RawMicrostructureEvent:
    payload = _mapping(event)
    if str(payload.get("e") or "") != "aggTrade":
        raise ValueError("unexpected Binance aggregate trade event type")
    symbol = _symbol(str(payload.get("s") or ""))
    trade_id = _positive_int(payload.get("a"), "a", allow_zero=True)
    trade_ms = _positive_int(payload.get("T") or payload.get("E"), "T")
    price = Decimal(str(payload.get("p")))
    quantity = Decimal(str(payload.get("q")))
    maker = payload.get("m")
    if price <= 0 or quantity <= 0 or not isinstance(maker, bool):
        raise ValueError("malformed Binance aggregate trade")
    return RawMicrostructureEvent(
        symbol=symbol,
        kind=RawMicrostructureKind.TRADE,
        exchange_at=datetime.fromtimestamp(trade_ms / 1000, tz=UTC),
        received_at=_utc(received_at),
        source="binance_spot_websocket",
        source_version=BINANCE_MICROSTRUCTURE_SOURCE_VERSION,
        payload_hash=_payload_hash(payload),
        raw_payload=payload,
        venue_sequence_start=trade_id,
        venue_sequence_end=trade_id,
        price=price,
        quantity=quantity,
        aggressor=TradeAggressor.SELL if maker else TradeAggressor.BUY,
    )


def _levels(value: object, *, allow_zero: bool) -> tuple[L2Level, ...]:
    if not isinstance(value, list):
        raise ValueError("Binance depth levels must be a list")
    levels: list[L2Level] = []
    for item in value:
        if not isinstance(item, list) or len(item) < 2:
            raise ValueError("malformed Binance depth level")
        price = Decimal(str(item[0]))
        quantity = Decimal(str(item[1]))
        if price <= 0 or quantity < 0 or (quantity == 0 and not allow_zero):
            raise ValueError("invalid Binance depth level")
        levels.append(L2Level(price=price, quantity=quantity))
    return tuple(levels)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    if hasattr(value, "model_dump"):
        result = value.model_dump(by_alias=True, exclude_none=True)
        return dict(result) if isinstance(result, Mapping) else {}
    raise ValueError("Binance event is not mapping-compatible")


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(value: object, field: str, *, allow_zero: bool = False) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Binance {field} must be an integer") from exc
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError(f"Binance {field} must be positive")
    return parsed


def _symbol(value: str) -> str:
    symbol = value.strip().upper()
    if (
        not symbol.endswith("USDT")
        or not symbol[:-4].isalnum()
        or len(symbol) > 20
    ):
        raise ValueError(f"unsupported Binance symbol: {value}")
    return symbol


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
