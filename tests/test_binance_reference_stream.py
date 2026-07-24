"""Official-SDK Binance reference stream normalization and lifecycle tests."""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from binance_sdk_spot.websocket_streams.models import KlineResponse

from crypto_threshold.adapters.prices import stream as stream_module
from crypto_threshold.adapters.prices.stream import (
    BinanceReferencePriceStream,
    normalize_binance_kline,
)

NOW = datetime(2026, 7, 23, 16, 1, tzinfo=UTC)
CLOSE_MS = int((NOW.timestamp() - 1) * 1000)


class FakeHandle:
    def __init__(self) -> None:
        self.callbacks: dict[str, Any] = {}
        self.unsubscribed = False

    def on(self, event: str, callback: Any) -> None:
        assert event == "message"
        self.callbacks[event] = callback

    async def unsubscribe(self) -> None:
        self.unsubscribed = True

    def emit(self, event: str, payload: object) -> None:
        self.callbacks[event](payload)


class FakeConnection:
    def __init__(self) -> None:
        self.handles: dict[str, FakeHandle] = {}
        self.closed = False
        self.kline_calls = 0

    async def kline(self, *, symbol: str, interval: object) -> FakeHandle:
        assert interval == "1m"
        self.kline_calls += 1
        handle = FakeHandle()
        self.handles[symbol] = handle
        return handle

    async def close_connection(self, *, close_session: bool) -> None:
        assert close_session
        self.closed = True


def test_normalizes_only_closed_one_minute_close_from_public_sdk_model() -> None:
    event = KlineResponse.from_dict(_event("BTCUSDT", "100001"))
    tick = normalize_binance_kline(event, received_at=NOW)
    assert tick is not None
    assert (tick.provider, tick.pair, tick.candle_interval, tick.price_field) == (
        "binance",
        "BTCUSDT",
        "1m",
        "Close",
    )
    assert tick.fresh
    assert tick.sequence == "123:9"
    assert tick.payload_hash
    open_event = _event("BTCUSDT", "100001")
    open_event["k"]["x"] = False
    assert normalize_binance_kline(open_event, received_at=NOW) is None
    future_event = _event("BTCUSDT", "100001")
    future_event["k"]["T"] = int((NOW + timedelta(minutes=1)).timestamp() * 1000)
    future_tick = normalize_binance_kline(future_event, received_at=NOW)
    assert future_tick is not None
    assert not future_tick.fresh


def test_start_stop_are_idempotent_and_queue_is_bounded_and_coalesced() -> None:
    connection = FakeConnection()

    async def factory() -> FakeConnection:
        return connection

    stream = BinanceReferencePriceStream(
        connection_factory=factory,
        clock=lambda: NOW,
        max_tick_slots=1,
    )
    stream.start()
    stream.start()
    _wait_for(lambda: len(connection.handles) == 2)
    connection.handles["btcusdt"].emit("message", _event("BTCUSDT", "100001"))
    connection.handles["btcusdt"].emit("message", _event("BTCUSDT", "100002"))
    connection.handles["ethusdt"].emit("message", _event("ETHUSDT", "3000"))
    ticks = stream.drain()
    health = stream.health()
    assert len(ticks) == 1
    assert ticks[0].pair == "ETHUSDT"
    assert health["detail"]["coalesced"] == 1
    assert health["detail"]["dropped"] == 1
    stream.stop()
    stream.stop()
    assert connection.closed
    assert stream.health()["status"] == "stopped"


def test_stream_uses_official_sdk_surface_and_no_private_or_direct_ws_import() -> None:
    source = inspect.getsource(stream_module)
    project = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert "binance_sdk_spot" in source
    assert "binance-sdk-spot==10.0.0" in project
    assert "polymarket._internal" not in source
    assert "import websockets" not in source
    assert '"websockets' not in project


def test_sdk_connection_receives_explicit_proxy_and_exact_interval(
    monkeypatch: object,
) -> None:
    import binance_sdk_spot.spot

    captured: dict[str, Any] = {}
    connection = object()

    class FakeStreams:
        async def create_connection(self) -> object:
            return connection

        async def close_connection(self, *, close_session: bool) -> None:
            raise AssertionError("successful connection must not be closed here")

    class FakeSpot:
        def __init__(self, *, config_ws_streams: object) -> None:
            captured["config"] = config_ws_streams
            self.websocket_streams = FakeStreams()

    monkeypatch.setattr(binance_sdk_spot.spot, "Spot", FakeSpot)
    stream = BinanceReferencePriceStream(
        proxy_url="http://127.0.0.1:12334",
    )
    assert asyncio.run(stream._create_connection()) is connection
    config = captured["config"]
    assert config.proxy == {
        "protocol": "http",
        "host": "127.0.0.1",
        "port": 12334,
    }
    assert stream._one_minute_interval() == "1m"
    health = stream.health()
    assert health["detail"]["proxy_enabled"] is True
    assert "127.0.0.1" not in str(health)


def test_stop_cancels_a_blocked_sdk_connection_attempt() -> None:
    async def blocked_factory() -> object:
        import asyncio

        await asyncio.Event().wait()
        return object()

    stream = BinanceReferencePriceStream(connection_factory=blocked_factory)
    stream.start()
    _wait_for(lambda: stream.health()["status"] == "connecting")
    started = time.monotonic()
    stream.stop()
    assert time.monotonic() - started < 2
    assert stream.health()["status"] == "stopped"


def test_stream_reconnects_when_subscribed_connection_stops_emitting() -> None:
    connection = FakeConnection()

    async def factory() -> FakeConnection:
        return connection

    stream = BinanceReferencePriceStream(
        connection_factory=factory,
        stale_seconds=0.05,
    )
    stream.start()
    _wait_for(lambda: stream.health()["detail"]["generation"] >= 2)
    stream.stop()
    assert connection.kline_calls >= 4


def _event(pair: str, close: str) -> dict[str, Any]:
    return {
        "e": "kline",
        "E": 123,
        "s": pair,
        "k": {
            "t": CLOSE_MS - 59_999,
            "T": CLOSE_MS,
            "s": pair,
            "i": "1m",
            "c": close,
            "L": 9,
            "x": True,
        },
    }


def _wait_for(predicate: Any) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("stream did not reach expected state")
