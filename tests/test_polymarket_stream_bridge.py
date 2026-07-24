"""Official-SDK stream bridge and crypto ladder behavior tests."""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from crypto_threshold.adapters.polymarket import stream as stream_mod
from crypto_threshold.adapters.polymarket.stream import (
    PolymarketStreamBridge,
    StreamQuote,
    StreamUserHint,
    normalize_sdk_event,
    normalize_sdk_events,
    select_stream_tokens,
)


class FakeHandle:
    def __init__(
        self,
        events: list[Any] | None = None,
        *,
        hang: bool = False,
        fail: bool = False,
    ) -> None:
        self.events = list(events or [])
        self.hang = hang
        self.fail = fail
        self.closed = False
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        if self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            await asyncio.sleep(0)
            return event
        if self.fail:
            self.fail = False
            raise RuntimeError("stream disconnected")
        if self.hang:
            while not self.closed:
                await asyncio.sleep(0.02)
        raise StopAsyncIteration

    def close(self) -> None:
        self.closed = True


class FakePublicClient:
    def __init__(self, *, finite_first: bool = False) -> None:
        self.finite_first = finite_first
        self.specs: list[Any] = []
        self.handles: list[FakeHandle] = []
        self.closed = False

    def subscribe(self, specs):
        self.specs.append(specs)
        finite = self.finite_first and not self.handles
        handle = FakeHandle(hang=not finite)
        self.handles.append(handle)
        return handle

    def close(self) -> None:
        self.closed = True
        for handle in self.handles:
            handle.close()


def _bbo(token: str, bid: str = "0.40", ask: str = "0.42") -> Any:
    return SimpleNamespace(
        topic="market",
        type="best_bid_ask",
        payload=SimpleNamespace(
            market="condition-1",
            token_id=token,
            best_bid=Decimal(bid),
            best_ask=Decimal(ask),
            spread=Decimal(ask) - Decimal(bid),
        ),
    )


def _quote(token: str, bid: str = "0.40", ask: str = "0.42") -> StreamQuote:
    bid_value = Decimal(bid)
    ask_value = Decimal(ask)
    return StreamQuote(
        token_id=token,
        best_bid=bid_value,
        best_ask=ask_value,
        midpoint=(bid_value + ask_value) / 2,
        spread=ask_value - bid_value,
        liquidity=None,
        received_at=time.monotonic(),
        source_type="best_bid_ask",
        condition_id="condition-1",
    )


def _market_rows() -> dict[str, dict[str, str]]:
    common = {
        "event_id": "event-1",
        "asset": "BTC",
        "settlement_provider": "binance",
        "pair": "BTC/USDT",
        "target_time_utc": "2026-07-23T16:00:00+00:00",
        "candle_interval": "1m",
        "price_field": "close",
    }
    return {
        "m1": {
            **common,
            "market_id": "m1",
            "condition_id": "c1",
            "yes_token_id": "y1",
            "no_token_id": "n1",
        },
        "m2": {
            **common,
            "market_id": "m2",
            "condition_id": "c2",
            "yes_token_id": "y2",
            "no_token_id": "n2",
        },
        "m3": {
            **common,
            "event_id": "event-2",
            "market_id": "m3",
            "condition_id": "c3",
            "yes_token_id": "y3",
            "no_token_id": "n3",
        },
    }


def test_normalizes_all_price_change_tokens_and_user_hints() -> None:
    event = SimpleNamespace(
        topic="market",
        type="price_change",
        payload=SimpleNamespace(
            market="condition-1",
            price_changes=(
                SimpleNamespace(token_id="yes", best_bid="0.4", best_ask="0.42"),
                SimpleNamespace(token_id="no", best_bid="0.58", best_ask="0.60"),
            ),
        ),
    )
    assert {item.token_id for item in normalize_sdk_events(event)} == {"yes", "no"}
    user = normalize_sdk_event(
        SimpleNamespace(
            topic="user",
            type="trade",
            payload=SimpleNamespace(status="MATCHED"),
        )
    )
    assert isinstance(user, StreamUserHint)
    assert user.kind == "trade"


def test_uses_only_official_public_sdk_surface() -> None:
    source = Path(stream_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not any(name == "websockets" or name.startswith("websockets.") for name in imports)
    assert not any(name.startswith("polymarket._internal") for name in imports)
    assert not any(name in {"sqlite3", "crypto_threshold.storage"} for name in imports)
    assert not any(name.startswith("crypto_threshold.storage.") for name in imports)

    from polymarket import AsyncPublicClient, AsyncSecureClient
    from polymarket.streams import MarketSpec, UserSpec

    assert AsyncPublicClient is not None
    assert AsyncSecureClient is not None
    assert MarketSpec(token_ids=["token"]).token_ids == ("token",)
    assert UserSpec().markets is None

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dependency_lines = [line.strip() for line in pyproject.read_text().splitlines()]
    assert not any(line.startswith('"websockets') for line in dependency_lines)


def test_start_stop_are_idempotent_and_market_spec_requests_bbo() -> None:
    client = FakePublicClient()
    bridge = PolymarketStreamBridge(public_client_factory=lambda: client)
    bridge.set_desired_tokens(["token"])
    bridge.start()
    first_thread = bridge._thread
    bridge.start()
    try:
        deadline = time.time() + 2
        while not client.specs and time.time() < deadline:
            time.sleep(0.02)
        assert bridge._thread is first_thread
        assert client.specs[-1].custom_feature_enabled is True
    finally:
        bridge.stop(timeout=2)
        bridge.stop(timeout=2)
    assert bridge.health().status == "disabled"


def test_restart_invalidates_old_freshness_and_requires_backfill() -> None:
    client = FakePublicClient()
    bridge = PolymarketStreamBridge(public_client_factory=lambda: client)
    bridge.set_desired_tokens(["token"])
    bridge._token_last_quote_at["token"] = time.monotonic()
    bridge.mark_rest_verified("token")
    bridge.start()
    try:
        assert bridge.needs_rest_backfill("token")
        assert not bridge.is_token_fresh("token")
        assert bridge.needs_rest_verification("token")
    finally:
        bridge.stop(timeout=2)


def test_duplicate_bbo_is_ignored_even_after_drain() -> None:
    bridge = PolymarketStreamBridge(public_client_factory=FakePublicClient)
    assert bridge._queue.publish(_quote("token")) is True
    assert set(bridge.drain().quotes) == {"token"}
    assert bridge._queue.publish(_quote("token")) is False
    assert bridge.drain().quotes == {}
    assert bridge.health().public_dict()["detail"]["unchanged_quotes"] == 1


def test_quote_queue_has_a_hard_bound() -> None:
    bridge = PolymarketStreamBridge(
        public_client_factory=FakePublicClient,
        max_quote_slots=2,
    )
    for token in ("t1", "t2", "t3"):
        bridge._queue.publish(_quote(token))
    batch = bridge.drain()
    assert set(batch.quotes) == {"t2", "t3"}
    assert batch.dropped == 1
    assert bridge.health().public_dict()["detail"]["remembered_quotes"] == 2


def test_token_reordering_does_not_resubscribe() -> None:
    bridge = PolymarketStreamBridge(public_client_factory=FakePublicClient)
    assert bridge.set_desired_tokens(["t2", "t1"], token_to_market={"t1": "m1"})
    generation = bridge.subscription_generation()
    assert not bridge.set_desired_tokens(["t1", "t2"], token_to_market={"t1": "m1"})
    assert bridge.subscription_generation() == generation


def test_new_token_and_disconnect_require_rest_backfill() -> None:
    bridge = PolymarketStreamBridge(public_client_factory=FakePublicClient)
    bridge._activate_subscription_tokens(("t1",))
    assert bridge.needs_rest_backfill("t1")
    bridge.mark_rest_verified("t1", now=10)
    bridge._activate_subscription_tokens(("t1", "t2"))
    assert not bridge.needs_rest_backfill("t1")
    assert bridge.needs_rest_backfill("t2")

    bridge._desired_tokens = ("t1", "t2")
    generation = bridge.subscription_generation()
    asyncio.run(bridge._read_handle(FakeHandle(fail=True), generation))
    assert bridge.needs_rest_backfill("t1")
    assert bridge.needs_rest_backfill("t2")


def test_reader_disconnect_automatically_resubscribes() -> None:
    client = FakePublicClient(finite_first=True)
    bridge = PolymarketStreamBridge(public_client_factory=lambda: client)
    bridge.set_desired_tokens(["t1"])
    bridge.start()
    try:
        deadline = time.time() + 3
        while len(client.handles) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert len(client.handles) >= 2
        assert bridge.needs_rest_backfill("t1")
    finally:
        bridge.stop(timeout=2)


def test_stale_and_expired_rest_verification_force_fallback() -> None:
    bridge = PolymarketStreamBridge(
        public_client_factory=FakePublicClient,
        stale_seconds=45,
        rest_verify_seconds=90,
    )
    bridge._status = "live"
    bridge._desired_tokens = ("t1",)
    bridge._token_last_quote_at["t1"] = 100
    bridge.mark_rest_verified("t1", now=100)
    assert not bridge.rest_fallback_active(now=120)
    assert bridge.rest_fallback_active(now=191)
    bridge.mark_rest_verified("t1", now=191)
    assert bridge.rest_fallback_active(now=146)


def test_incomplete_bbo_triggers_hint_but_never_marks_token_fresh() -> None:
    bridge = PolymarketStreamBridge(public_client_factory=FakePublicClient)
    bridge._status = "live"
    bridge._desired_tokens = ("t1",)
    bridge.mark_rest_verified("t1")
    incomplete = StreamQuote(
        token_id="t1",
        best_bid=Decimal("0.4"),
        best_ask=None,
        midpoint=None,
        spread=None,
        liquidity=None,
        received_at=time.monotonic(),
        source_type="best_bid_ask",
    )
    assert bridge._queue.publish(incomplete)
    assert set(bridge.drain().quotes) == {"t1"}
    assert not bridge.is_token_fresh("t1")
    assert bridge.rest_fallback_active()


def test_user_events_only_coalesce_to_reconciliation_hint() -> None:
    bridge = PolymarketStreamBridge(public_client_factory=FakePublicClient)
    bridge._queue.publish(StreamUserHint(kind="order", received_at=1))
    bridge._queue.publish(StreamUserHint(kind="trade", received_at=2))
    batch = bridge.drain()
    assert batch.reconcile_due is True
    assert batch.coalesced >= 1
    assert not hasattr(bridge, "save_fill")
    assert not hasattr(bridge, "place_order")


def test_startup_failure_degrades_without_raising() -> None:
    def fail() -> Any:
        raise RuntimeError("cannot open public stream")

    bridge = PolymarketStreamBridge(public_client_factory=fail)
    bridge.set_desired_tokens(["t1"])
    bridge.start()
    try:
        deadline = time.time() + 2
        while bridge.health().status == "connecting" and time.time() < deadline:
            time.sleep(0.02)
        assert bridge.health().status == "degraded"
        assert bridge.rest_fallback_active()
    finally:
        bridge.stop(timeout=2)


def test_health_recursively_scrubs_secrets_and_exception_text() -> None:
    bridge = PolymarketStreamBridge(
        private_key="PRIVATE-MATERIAL",
        api_credentials="API-CREDENTIAL",
        public_client_factory=FakePublicClient,
    )
    bridge._set_status("degraded", "private_key=PRIVATE-MATERIAL")
    health = bridge.health()
    health.detail["nested"] = {"api_key": "API-CREDENTIAL", "safe": "ok"}
    blob = str(health.public_dict())
    assert "PRIVATE-MATERIAL" not in blob
    assert "API-CREDENTIAL" not in blob
    assert "api_key" not in blob
    assert "safe" in blob


def test_crypto_selector_uses_complete_event_ladders_without_weather_fields() -> None:
    rows = _market_rows()
    desired = select_stream_tokens(
        positions=[],
        open_orders=[],
        active_market_ids=["m1"],
        ranked_candidates=[{"market_id": "m3", "net_ev": "0.1"}],
        market_rows=rows,
        candidate_group_cap=1,
    )
    assert desired.active_ladder_tokens == {"y1", "n1", "y2", "n2"}
    assert desired.candidate_tokens == {"y3", "n3"}
    assert desired.selected_ladders[0] == ("event", "event-1")
    assert "city" not in stream_mod.crypto_ladder_key.__code__.co_names
    assert "target_date" not in stream_mod.crypto_ladder_key.__code__.co_names


def test_held_and_open_order_tokens_are_not_displaced_by_candidate_cap() -> None:
    desired = select_stream_tokens(
        positions=[{"market_id": "m1", "outcome": "NO", "size": "2"}],
        open_orders=[{"market_id": "m2", "token_id": "y2", "size": "1"}],
        active_market_ids=[],
        ranked_candidates=[{"market_id": "m3"}],
        market_rows=_market_rows(),
        candidate_group_cap=0,
    )
    assert desired.token_ids == ("n1", "y2")
    assert desired.held_tokens == {"n1"}
    assert desired.open_order_tokens == {"y2"}


def test_health_reader_is_thread_safe_under_parallel_access() -> None:
    bridge = PolymarketStreamBridge(public_client_factory=FakePublicClient)
    errors: list[Exception] = []

    def read_health() -> None:
        try:
            for _ in range(50):
                bridge.health().public_dict()
        except Exception as exc:  # pragma: no cover - assertion captures race only
            errors.append(exc)

    threads = [threading.Thread(target=read_health) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_stream_quote_received_at_uses_monotonic_clock() -> None:
    before = time.monotonic()
    quote = normalize_sdk_event(_bbo("token"))
    after = time.monotonic()
    assert isinstance(quote, StreamQuote)
    assert before <= quote.received_at <= after
    assert datetime.now(UTC).tzinfo is UTC
