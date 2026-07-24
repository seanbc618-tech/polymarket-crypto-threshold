"""Stream hints remain subordinate to the existing read-only REST workflow."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from pathlib import Path

from crypto_threshold.adapters.polymarket.stream import PolymarketStreamBridge, StreamQuote
from crypto_threshold.adapters.polymarket.translator import translate_market
from crypto_threshold.config import Settings
from crypto_threshold.domain.rules import parse_contract
from crypto_threshold.services.market_workflow_service import MarketWorkflowService
from crypto_threshold.services.stream_research_service import StreamResearchCoordinator
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository
from tests.conftest import (
    NOW,
    FakeBinanceProvider,
    FakeCoinbaseProvider,
    FakePolymarketClient,
    make_market_payload,
)


def _seed(repository: Repository, payload: dict[str, object]) -> None:
    market = translate_market(payload, received_at=NOW)
    repository.upsert_market(market)
    rule = parse_contract(market, now=NOW)
    assert rule.tradable
    repository.save_resolution_rule(market.market_id, rule, observed_at=NOW, received_at=NOW)


def _quote(token_id: str) -> StreamQuote:
    return StreamQuote(
        token_id=token_id,
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        liquidity=None,
        received_at=1,
        source_type="best_bid_ask",
    )


def test_changed_bbo_expands_to_complete_event_and_duplicate_does_not_reprice(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "stream.db")
    database.initialize()
    repository = Repository(database)
    first = make_market_payload()
    second = make_market_payload(
        id="market-2",
        conditionId="condition-2",
        clobTokenIds='["yes-2", "no-2"]',
        question="Will Bitcoin be above $110,000 on July 23, 2026?",
    )
    _seed(repository, first)
    _seed(repository, second)
    bridge = PolymarketStreamBridge(public_client_factory=lambda: None)
    coordinator = StreamResearchCoordinator(repository=repository, bridge=bridge)
    desired = coordinator.sync_subscriptions(active_market_ids=("market-1",))
    assert set(desired.token_ids) == {"yes-token", "no-token", "yes-2", "no-2"}

    assert bridge._queue.publish(_quote("yes-token"))
    first_pulse = coordinator.pulse()
    assert set(first_pulse.reprice_market_ids) == {"market-1", "market-2"}
    assert not bridge._queue.publish(_quote("yes-token"))
    assert coordinator.pulse().reprice_market_ids == ()


def test_fresh_stream_never_replaces_final_rest_order_books(
    tmp_path: Path,
    market_payload: dict[str, object],
) -> None:
    database = Database(tmp_path / "workflow-stream.db")
    database.initialize()
    repository = Repository(database)
    client = FakePolymarketClient(market_payload)
    bridge = PolymarketStreamBridge(public_client_factory=lambda: None)
    coordinator = StreamResearchCoordinator(repository=repository, bridge=bridge)
    bridge._status = "live"
    bridge.set_desired_tokens(["yes-token", "no-token"])
    bridge._token_last_quote_at = {"yes-token": 100, "no-token": 100}
    bridge.mark_rest_verified("yes-token", now=100)
    bridge.mark_rest_verified("no-token", now=100)
    service = MarketWorkflowService(
        client=client,
        repository=repository,
        binance=FakeBinanceProvider(),
        coinbase=FakeCoinbaseProvider(),
        settings=Settings(DATABASE_PATH=str(database.path), _env_file=None),
        clock=lambda: NOW,
        stream_coordinator=coordinator,
    )

    signal = service.analyze("market-1")

    assert signal.status == "analyzed"
    assert client.reads == ["market", "book:yes-token", "book:no-token", "market_info"]
    assert not bridge.needs_rest_verification("yes-token")
    assert not bridge.needs_rest_verification("no-token")


def test_stream_start_failure_does_not_kill_rest_analysis(
    tmp_path: Path,
    market_payload: dict[str, object],
) -> None:
    database = Database(tmp_path / "fallback.db")
    database.initialize()
    repository = Repository(database)

    def fail():
        raise RuntimeError("stream unavailable")

    bridge = PolymarketStreamBridge(public_client_factory=fail)
    coordinator = StreamResearchCoordinator(repository=repository, bridge=bridge)
    assert coordinator.start()
    client = FakePolymarketClient(market_payload)
    service = MarketWorkflowService(
        client=client,
        repository=repository,
        binance=FakeBinanceProvider(),
        coinbase=FakeCoinbaseProvider(),
        settings=Settings(DATABASE_PATH=str(database.path), _env_file=None),
        clock=lambda: NOW,
        stream_coordinator=coordinator,
    )
    try:
        assert service.analyze("market-1").status == "analyzed"
        assert "book:yes-token" in client.reads
        assert "book:no-token" in client.reads
    finally:
        coordinator.stop()


def test_disabled_stream_pulse_and_once_analysis_remain_operational(
    tmp_path: Path,
    market_payload: dict[str, object],
) -> None:
    database = Database(tmp_path / "disabled.db")
    database.initialize()
    repository = Repository(database)
    coordinator = StreamResearchCoordinator(repository=repository, bridge=None)
    assert coordinator.pulse().status == "disabled"
    service = MarketWorkflowService(
        client=FakePolymarketClient(market_payload),
        repository=repository,
        binance=FakeBinanceProvider(),
        coinbase=FakeCoinbaseProvider(),
        settings=Settings(DATABASE_PATH=str(database.path), _env_file=None),
        clock=lambda: NOW,
    )
    pulse, signals = service.analyze_stream_pulse()
    assert pulse is None
    assert signals == ()
    assert service.analyze("market-1").status == "analyzed"


def test_user_hint_has_no_fill_or_order_persistence_surface(tmp_path: Path) -> None:
    database = Database(tmp_path / "no-mutation.db")
    database.initialize()
    repository = Repository(database)
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "fills" not in tables
    assert "orders" not in tables
    assert not hasattr(repository, "save_fill")
    assert not hasattr(repository, "save_order")


def test_reference_price_stream_contract_is_separate_and_timestamped() -> None:
    from crypto_threshold.adapters.prices.stream import ReferencePriceTick

    tick = ReferencePriceTick(
        provider="binance",
        pair="BTC/USDT",
        candle_interval="1m",
        price_field="close",
        price=Decimal("100000"),
        provider_timestamp=NOW,
        received_at=NOW.astimezone(UTC),
        fresh=True,
        sequence="123",
        payload_hash="sha256:test",
    )
    assert tick.provider == "binance"
    assert tick.pair == "BTC/USDT"
    assert tick.provider_timestamp.tzinfo is UTC
