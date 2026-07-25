"""Official-SDK Chainlink reference stream behavior."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crypto_threshold.adapters.prices.chainlink_stream import (
    ChainlinkReferencePriceStream,
    normalize_chainlink_price,
)
from crypto_threshold.domain.assets import SUPPORTED_CHAINLINK_PAIRS

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _event(pair: str, observed_at: datetime, value: str = "100") -> dict[str, Any]:
    return {
        "topic": "prices.crypto.chainlink",
        "type": "update",
        "timestamp": int(observed_at.timestamp() * 1000),
        "payload": {
            "symbol": pair.lower(),
            "timestamp": int(observed_at.timestamp() * 1000),
            "value": value,
        },
    }


@pytest.mark.parametrize("pair", sorted(SUPPORTED_CHAINLINK_PAIRS))
def test_normalizes_all_seven_supported_chainlink_pairs(pair: str) -> None:
    tick = normalize_chainlink_price(
        _event(pair, NOW),
        received_at=NOW + timedelta(milliseconds=100),
    )

    assert tick is not None
    assert tick.provider == "chainlink"
    assert tick.pair == pair
    assert tick.candle_interval == "tick"
    assert tick.price_field == "value"
    assert tick.fresh
    assert tick.provider_timestamp == NOW
    assert tick.received_at == NOW + timedelta(milliseconds=100)
    assert tick.sequence == str(int(NOW.timestamp() * 1000))
    assert tick.payload_hash is not None and len(tick.payload_hash) == 64


def test_rejects_unknown_stale_and_malformed_events() -> None:
    assert (
        normalize_chainlink_price(
            _event("ADA/USD", NOW),
            received_at=NOW,
        )
        is None
    )
    stale = normalize_chainlink_price(
        _event("BTC/USD", NOW),
        received_at=NOW + timedelta(seconds=6),
    )
    assert stale is not None and not stale.fresh
    assert (
        normalize_chainlink_price(
            SimpleNamespace(
                topic="prices.crypto.chainlink",
                payload=SimpleNamespace(
                    symbol="btc/usd",
                    timestamp="bad",
                    value="100",
                ),
            ),
            received_at=NOW,
        )
        is None
    )


class _Handle:
    def __init__(self, events: list[Any], *, hold_open: bool = False) -> None:
        self.events = list(events)
        self.hold_open = hold_open
        self.closed = False

    async def __anext__(self) -> Any:
        if self.events:
            return self.events.pop(0)
        if self.hold_open:
            await asyncio.sleep(60)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for stream state")


def test_lifecycle_is_idempotent_bounded_coalescing_and_reconnects() -> None:
    calls = 0
    clients: list[_Client] = []
    handles: list[_Handle] = []

    async def factory(_pairs: tuple[str, ...]) -> tuple[_Client, _Handle]:
        nonlocal calls
        calls += 1
        client = _Client()
        base = NOW + timedelta(seconds=calls * 10)
        events = [
            _event("BTC/USD", base + timedelta(seconds=index), str(100 + index))
            for index in range(5)
        ]
        handle = _Handle(events, hold_open=calls >= 2)
        clients.append(client)
        handles.append(handle)
        return client, handle

    clock_value = NOW + timedelta(seconds=30)
    stream = ChainlinkReferencePriceStream(
        pairs=("BTC/USD",),
        stale_seconds=60,
        history_seconds=120,
        max_ticks_per_pair=3,
        subscription_factory=factory,
        clock=lambda: clock_value,
    )
    stream.start()
    stream.start()
    _wait_until(lambda: int(stream.health()["detail"]["generation"]) >= 2)
    _wait_until(lambda: len(stream.history("BTC/USD")) == 3)

    drained = stream.drain()
    assert len(drained) == 1
    assert drained[0].price >= 100
    assert len(stream.history("BTC/USD")) == 3
    health = stream.health()
    assert health["status"] == "connected"
    assert int(health["detail"]["generation"]) >= 2
    assert int(health["detail"]["dropped"]) >= 2
    assert int(health["detail"]["coalesced"]) >= 1
    assert stream.latest_tick("BTC/USD", at=clock_value) is not None

    stream.stop()
    stream.stop()
    assert stream.health()["status"] == "stopped"
    assert all(client.closed for client in clients)
    assert all(handle.closed for handle in handles)


def test_health_never_copies_exception_text_or_secret_material() -> None:
    async def failing_factory(
        _pairs: tuple[str, ...],
    ) -> tuple[_Client, _Handle]:
        raise RuntimeError("private_key=do-not-leak api_secret=do-not-leak")

    stream = ChainlinkReferencePriceStream(
        pairs=("BTC/USD",),
        stale_seconds=1,
        subscription_factory=failing_factory,
        clock=lambda: NOW,
    )
    stream.start()
    _wait_until(lambda: stream.health()["status"] == "degraded")
    rendered = repr(stream.health()).lower()
    stream.stop()

    assert "do-not-leak" not in rendered
    assert "private_key" not in rendered
    assert "api_secret" not in rendered


def test_runtime_source_uses_only_official_public_sdk_surface() -> None:
    source_path = (
        Path(__file__).parents[1]
        / "src"
        / "crypto_threshold"
        / "adapters"
        / "prices"
        / "chainlink_stream.py"
    )
    source = source_path.read_text(encoding="utf-8")
    assert "from polymarket import AsyncPublicClient" in source
    assert "from polymarket.streams import CryptoPricesSpec" in source
    assert "websockets" not in source
    assert "polymarket._internal" not in source
